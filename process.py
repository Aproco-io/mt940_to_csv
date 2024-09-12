import os
import csv
import json
# from typing import io

# import ET
import pandas as pd
import aiofiles
import docx
import openai
import pdfplumber
import requests
from openai import OpenAI
from openai.types import file_content
from progress.spinner import MoonSpinner, PixelSpinner
from dotenv import load_dotenv



NBP_API_URL = "http://api.nbp.pl/api/exchangerates/rates/A/{}/{}/"

load_dotenv()
OPENAI_API_KEY = os.getenv('OPEN_AI_API_KEY')
APP_API_KEY=os.getenv('APP_API_KEY')
client = OpenAI(api_key=OPENAI_API_KEY)



def get_exchange_rate(currency, date):
    """
    Retrieves exchange rate for a given currency and date from the NBP API.

    Args:
      currency: Currency code (e.g. USD, EUR).
      date: Date in the format YYYY-MM-DD.

    Returns:
      Exchange rate for the given currency and date according to PLN.
    """
    url = NBP_API_URL.format(currency, date)
    try:
        response = requests.get(url)
        decoded_data = response.text.encode().decode("utf-8-sig")
        data = json.loads(decoded_data)
        return data["rates"][0]["mid"]
    except Exception as e:
        print(f"Error while fetching exchange rate ❌: {response}")
        print(url)
        return None


def parse_mt940(filename):
    """
    Parses a PKO BP MT940 file and returns a list of transactions.

    Args:
      filename: Path to the MT940 file.

    Returns:
      A list of dictionaries containing transaction information.
    """
    with open(filename, "r") as f:
        lines = f.readlines()

    # Initialize variables
    transactions = []
    account = None
    transaction_date = None
    transaction_amount = None
    transaction_currency = None
    transaction_currency_rate = None
    transaction_id = None
    transaction_title = ""
    with MoonSpinner(" Processing ") as bar:
        for line in lines:
            if line.startswith(":25:"):
                # Account name
                account = line[5:7].strip() + " " + line[7:].strip()
            elif line.startswith(":60F:"):
                # Transaction currency
                transaction_currency = line[12:15:].strip()
            elif line.startswith(":61:"):
                # Transaction date
                transaction_date = (
                    line[8:10].strip()
                    + "-"
                    + line[6:8].strip()
                    + "-"
                    + "20"
                    + line[4:6].strip()
                )
                # Transaction amount
                transaction_sign = "+" if line[14].strip() == "D" else "-"
                transaction_amount = transaction_sign + line.split("N")[0][
                    15:
                ].strip().replace(",", ".")

                date = (
                    "20"
                    + line[4:6].strip()
                    + "-"
                    + line[6:8].strip()
                    + "-"
                    + line[8:10].strip()
                )
                transaction_currency_rate = get_exchange_rate(
                    transaction_currency, date
                )

            elif line.startswith(":86:"):
                # Transaction id
                transaction_id = line[10:].strip()
            elif line.startswith("~"):
                subfield = int(line[1:3])
                if subfield > 19 and subfield < 26:
                    transaction_title = transaction_title + line[3:].strip().replace(
                        "˙", ""
                    )

            # Save transaction to list
            if line.startswith("~63"):
                transactions.append(
                    {
                        "account": account,
                        "transaction_date": transaction_date,
                        "transaction_amount": transaction_amount,
                        "transaction_currency": transaction_currency,
                        "transaction_currency_rate": transaction_currency_rate,
                        "transaction_id": transaction_id,
                        "transaction_title": " ".join(transaction_title.split()),
                    }
                )
                transaction_title = ""
            bar.next()
        return transactions


def load_files_from_folder(folder_path: str) -> list:
    # Lista, która będzie zawierała ścieżki do plików
    file_list = []

    # Przejście przez wszystkie pliki w folderze
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            # Pobieranie pełnej ścieżki do pliku
            full_path = os.path.join(root, file)
            file_list.append(full_path)

    return file_list

# def match_invoices_with_transactions(invoices, transactions):
#     matched = []
#     unmatched = []
#     for invoice in invoices:
#         match = None
#         for transaction in transactions:
#             if (invoice['Transaction ID'] in transaction['Transaction title'] and
#                 invoice['Transaction amount'] == abs(transaction['Transaction amount'])):
#                 match = transaction
#                 break
#         if match:
#             matched.append((invoice, match))
#         else:
#             unmatched.append(invoice)
#     return matched, unmatched


def send_documents(file_paths):
    """
    Wysyła listę plików (faktur lub innych dokumentów) do lokalnego serwera na porcie 8080, pod endpoint 'process-file-mt940'.

    :param file_paths: Lista ścieżek do plików dokumentów
    :return: Odpowiedź serwera na zapytanie POST
    """
    url = 'http://localhost:8000/process-file-mt940'
    # Nagłówki z kluczem API
    headers = {
        'api-key': APP_API_KEY,
        'Accept': '*/*',
        'Connection': 'keep-alive'
    }

    fields = [
        "Account number",
        "Transaction date",
        "Amount",
        "Currency",
        "Transaction title",
        "Invoice number"
    ]

    # Przygotowanie plików do wysłania
    files = [('file', (open(file_path, 'rb'))) for file_path in file_paths]

    # Dane do wysłania w body zapytania
    data = {
        'fields': fields,  # Możesz dostosować to pole zgodnie z wymaganiami
        'document_type': 'invoice'
    }

    # Wysłanie zapytania POST z plikami i dodatkowymi danymi
    response = requests.post(url, headers=headers, files=files, data=data)

    # Zamknięcie plików po wysłaniu
    for file_tuple in files:
        file_tuple[1].close()

    # Zwrócenie odpowiedzi z serwera
    return response


def match_mt940_with_invoices(mt940_csv_path, invoices_json):
    # Odczytanie pliku MT940 CSV do DataFrame
    mt940_df = pd.read_csv(mt940_csv_path, sep='|', index_col=False)

    # Tworzenie pustej listy, aby zapisać transakcje z informacją o dopasowaniu
    all_transactions = []

    # --- Sprawdzanie dopasowania numeru faktury oraz kwoty i waluty ---
    for idx, row in mt940_df.iterrows():
        title = row['Transaction title']  # Pobieranie tytułu transakcji
        try:
            amount = float(row['Transaction amount'])  # Konwersja kwoty na float
        except ValueError:
            amount = None  # Ustawienie na None, jeśli konwersja nieudana
        currency = row['Transaction currency'].strip()  # Waluta transakcji
        match_found = False  # Flaga dla dopasowania
        invoice_number_match = ""  # Zmienna na numer faktury
        invoice_filename = ""  # Zmienna na nazwę pliku faktury
        invoice_details = {}  # Zmienna na dodatkowe informacje z faktury

        # Iteracja po wszystkich fakturach w invoices_json
        for invoice in invoices_json:
            invoice_number = invoice['extracted_fields']['Invoice number']
            try:
                invoice_amount = float(invoice['extracted_fields']['Amount'])  # Konwersja kwoty na float
            except ValueError:
                invoice_amount = None  # Ustawienie na None, jeśli konwersja nieudana
            invoice_currency = invoice['extracted_fields']['Currency'].strip()

            # Sprawdzanie, czy numer faktury znajduje się w tytule
            if invoice_number in title:
                match_found = True  # Numer faktury dopasowany
                invoice_number_match = invoice_number  # Zapisanie numeru faktury
                invoice_filename = invoice['filename']  # Zapisanie nazwy pliku
                invoice_details = invoice['extracted_fields']  # Zapisanie szczegółów faktury

            # Sprawdzanie, czy kwota i waluta są zgodne
            if amount == invoice_amount and currency == invoice_currency:
                match_found = True  # Kwota i waluta dopasowane
                invoice_number_match = invoice_number  # Zapisanie numeru faktury
                invoice_filename = invoice['filename']  # Zapisanie nazwy pliku
                invoice_details = invoice['extracted_fields']  # Zapisanie szczegółów faktury

        # Tworzenie rekordu z dodatkowymi informacjami o fakturze, jeśli dopasowanie
        matched_record = {
            'Account': row['Account'],
            'Transaction date': row['Transaction date'],
            'Transaction amount': row['Transaction amount'],
            'Transaction currency': row['Transaction currency'],
            'Transaction ID': row['Transaction ID'],
            'Transaction title': title,
            'Match': 'yes' if match_found else 'no',
            'Invoice filename': invoice_filename,
            'Invoice number': invoice_number_match,
            'Invoice account number': invoice_details.get('Account number', ''),
            'Invoice transaction date': invoice_details.get('Transaction date', ''),
            'Invoice transaction title': invoice_details.get('Transaction title', ''),
            'Invoice amount': invoice_details.get('Amount', ''),
            'Invoice currency': invoice_details.get('Currency', '')
        }

        # Dodanie rekordu do listy
        all_transactions.append(matched_record)

    # Tworzenie DataFrame z listy transakcji
    all_transactions_df = pd.DataFrame(all_transactions)

    # Zmiana kolejności kolumn
    all_transactions_df = all_transactions_df[[
        'Account', 'Transaction date', 'Transaction amount', 'Transaction currency',
        'Transaction ID', 'Transaction title', 'Match',
        'Invoice filename', 'Invoice number', 'Invoice amount', 'Invoice currency',
        'Invoice transaction date', 'Invoice transaction title', 'Invoice account number'
    ]]

    # Sortowanie: transakcje z dopasowaniem ("yes") na samej górze
    all_transactions_df = all_transactions_df.sort_values(by='Match', ascending=False)

    # Zapis do Excela z kolorowaniem, centrowaniem i dodawaniem podsumowania
    with pd.ExcelWriter('matched_transactions.xlsx', engine='xlsxwriter') as writer:
        # Zapis DataFrame do Excela
        all_transactions_df.to_excel(writer, index=False, sheet_name='Matches')

        # Po zapisaniu danych do Excela, pobieramy workbook i worksheet
        workbook  = writer.book
        worksheet = writer.sheets['Matches']

        # Stworzenie formatu do centrowania komórek
        center_format = workbook.add_format({'align': 'center', 'valign': 'vcenter'})

        # Kolorowanie komórek na podstawie dopasowania
        for idx, col in enumerate(all_transactions_df.columns):
            worksheet.conditional_format(1, idx, len(all_transactions_df), idx, {
                'type': 'cell',
                'criteria': 'equal to',
                'value': '"yes"',
                'format': workbook.add_format({'bg_color': '#90EE90', 'align': 'center', 'valign': 'vcenter'})
            })
            worksheet.conditional_format(1, idx, len(all_transactions_df), idx, {
                'type': 'cell',
                'criteria': 'equal to',
                'value': '"no"',
                'format': workbook.add_format({'bg_color': '#FF474C', 'align': 'center', 'valign': 'vcenter'})
            })

        # Automatyczne rozsunięcie kolumn na podstawie szerokości danych i centrowanie
        for i, col in enumerate(all_transactions_df.columns):
            max_len = max(all_transactions_df[col].astype(str).map(len).max(), len(col)) + 2  # +2 for padding
            worksheet.set_column(i, i, max_len, center_format)  # Dodanie center_format do każdej kolumny

        # Wywołanie funkcji summary i zapisanie podsumowania w kolejnej zakładce
        summary(all_transactions_df, writer)

    print("Wyniki zostały zapisane do pliku Excel z kolorowaniem i dostosowaną szerokością kolumn.")


def summary(df, writer):
    """
    Function to generate transaction summaries for each account and save to an Excel file.

    Parameters:
    df (DataFrame): The DataFrame containing transaction data.
    writer (pd.ExcelWriter): ExcelWriter object to write to the same file.
    """

    # Convert 'Transaction amount' to numeric, ensuring proper handling of debits and credits
    df['Transaction amount'] = pd.to_numeric(df['Transaction amount'], errors='coerce')

    # Create the summary DataFrame and include the 'Currency' column next to 'Account'
    summary_df = df.groupby(['Account', 'Transaction currency']).agg(
        total_transactions=('Transaction amount', 'count'),
        total_credit=('Transaction amount', lambda x: x[x > 0].sum()),  # Total of positive amounts (credits)
        total_debit=('Transaction amount', lambda x: x[x < 0].sum()),  # Total of negative amounts (debits)
        credit_count=('Transaction amount', lambda x: (x > 0).sum()),  # Count of positive amounts (credits)
        debit_count=('Transaction amount', lambda x: (x < 0).sum())  # Count of negative amounts (debits)
    ).reset_index()

    # Calculate the final balance (kwota ostateczna)
    summary_df['final_balance'] = summary_df['total_credit'] + summary_df['total_debit']  # Add credits and debits

    # Rename 'Transaction currency' to 'Account currency'
    summary_df = summary_df.rename(columns={'Transaction currency': 'Account currency'})

    # Rename columns as per the desired output
    summary_df = summary_df.rename(columns={
        'Account': 'Account',
        'Transaction currency': 'Account currency',
        'total_credit': 'Total credit',
        'total_debit': 'Total debit',
        'final_balance': 'Final balance',
        'total_transactions': 'Total transactions',
        'credit_count': 'Credit count',
        'debit_count': 'Debit count'
    })

    # Reorganize columns in the desired order
    summary_df = summary_df[[
        'Account', 'Account currency', 'Total credit', 'Total debit', 'Final balance',
        'Total transactions', 'Credit count', 'Debit count'
    ]]

    # Write the summary to a new sheet in the same Excel file
    summary_df.to_excel(writer, sheet_name='Summary', index=False)

    # Get the workbook and worksheet to adjust column widths
    workbook = writer.book
    worksheet = writer.sheets['Summary']

    # Adjust column widths based on the longest value in each column
    for i, col in enumerate(summary_df.columns):
        max_len = max(summary_df[col].astype(str).map(len).max(), len(col)) + 2  # +2 for padding
        worksheet.set_column(i, i, max_len)

    print(f"Podsumowanie zapisano w arkuszu 'Summary' z dostosowaną szerokością kolumn.")


def match_invoices_with_mt940(mt940_csv_path, invoices_json):
    """
    Dopasowuje dane z wyciągu MT940 (z pliku CSV) do faktur (z formatu JSON), korzystając z modelu GPT-4 do przetwarzania danych faktur.

    :param mt940_csv_path: Ścieżka do pliku CSV zawierającego dane MT940
    :param invoices_json: JSON zawierający dane faktur
    :param api_key: Klucz API OpenAI do komunikacji z GPT-4
    :return: Wynik dopasowania
    """

    # Odczytanie pliku MT940 CSV do DataFrame
    mt940_df = pd.read_csv(mt940_csv_path, sep='|')

    # Ustawienie wyświetlania wszystkich wierszy
    pd.set_option('display.max_rows', None)

    # Ustawienie wyświetlania wszystkich kolumn
    pd.set_option('display.max_columns', None)

    with open("prompt.txt", 'r') as file:
        prompt = file.read()

    content = f"Invoices:\n{invoices_json}\n\nMT940 transactions:{mt940_df.to_string()}"

    with open('content.txt', 'w', encoding='utf-8') as file:
        file.write(content)

    # response = client.chat.completions.create(
    #     model="gpt-4o",
    #     messages=[
    #         {"role": "system", "content": prompt},
    #         {"role": "user", "content": content}
    #     ],
    #     response_format={"type": "text"},
    #     temperature=0
    # )

    # result = response.choices[0].message.content

    result = 0

    return result


def main():
    """
    Processes all MT940 files in the data folder and saves the data to the results.csv file.
    """
    files = os.listdir("data")
    transactions = []
    file_list = load_files_from_folder("documents/")

    with open('invoices_from_response.json', 'r', encoding='utf-8') as f:
        data = json.load(f)

    mt940_csv_path = r'results.csv'
    match_mt940_with_invoices(mt940_csv_path, data)

    # # Wywołanie funkcji i sprawdzenie odpowiedzi
    # response = send_documents(file_list)
    #
    # # Konwersja response.text na obiekt Pythonowy (listę)
    # data = json.loads(response.text)
    #
    # # Zapis danych do pliku JSON
    # with open('invoices_from_response.json', 'w') as json_file:
    #     json.dump(data, json_file, indent=4)
    #
    # with open('invoices_from_response.json', 'r', encoding='utf-8') as f:
    #     data = json.load(f)
    #
    # for file in files:
    #     if file.lower().endswith(".txt"):
    #         print("File: " + file)
    #         transactions += parse_mt940(os.path.join("data", file))
    #         print("  Done ✔️")
    #
    # with open("results.csv", "w", newline="") as f:
    #     writer = csv.writer(f, delimiter="|")
    #     writer.writerow(
    #         [
    #             "Account",
    #             "Transaction date",
    #             "Transaction amount",
    #             "Transaction currency",
    #             "Transaction currency rate",
    #             "Transaction ID",
    #             "Transaction title",
    #         ]
    #     )
    #     for transaction in transactions:
    #         writer.writerow(
    #             [
    #                 transaction["account"],
    #                 transaction["transaction_date"],
    #                 transaction["transaction_amount"],
    #                 transaction["transaction_currency"],
    #                 transaction["transaction_currency_rate"],
    #                 transaction["transaction_id"],
    #                 transaction["transaction_title"],
    #             ]
    #         )
    #
    # mt940_csv_path = r'results.csv'
    # print("The report is being generated.")
    # raport = match_invoices_with_mt940(mt940_csv_path, data)
    #
    # with open('raport.csv', 'w', newline='', encoding='utf-8') as file:
    #     file.write(raport)
    #
    # print("The report has been generated.")

if __name__ == "__main__":
    main()