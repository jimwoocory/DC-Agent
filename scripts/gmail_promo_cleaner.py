import imaplib
import os

# Load SMTP_PASS from the env file if not provided in environment
ENV_FILE = "/Users/dianchi/.config/nas_sync/smtp.env"


def load_env():
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    os.environ[key] = value


def clean_promotions():
    load_env()

    user = os.getenv("SMTP_FROM", "jimwoo.cory@gmail.com")
    password = os.getenv("SMTP_PASS")
    host = "imap.gmail.com"

    if not password:
        print("Error: SMTP_PASS not found.")
        return

    try:
        mail = imaplib.IMAP4_SSL(host)
        mail.login(user, password)

        # Try to select the All Mail folder or INBOX
        # We'll iterate through folders to find the one with the \All flag
        all_mail_folder = "INBOX"  # Fallback
        status, folders = mail.list()
        if status == "OK":
            for folder in folders:
                folder_str = folder.decode()
                if "\\All" in folder_str:
                    # Extract folder name from the string like: (\All \HasNoChildren) "/" "[Gmail]/&YkBnCZCuTvY-"
                    all_mail_folder = folder_str.split('"/"')[-1].strip().strip('"')
                    break

        print(f"Selecting folder: {all_mail_folder}")
        res, _ = mail.select(all_mail_folder)
        if res != "OK":
            print(f"Failed to select {all_mail_folder}, falling back to INBOX")
            mail.select("INBOX")

        print(f"Searching for promotional emails for {user}...")
        status, data = mail.search(None, "X-GM-RAW", "category:promotions")

        if status != "OK":
            print("X-GM-RAW search failed, trying simple search...")
            # Fallback: just search for emails with "unsubscribe" or similar if needed,
            # but usually X-GM-RAW is supported on Gmail.
            return

        mail_ids = data[0].split()
        count = len(mail_ids)

        if count == 0:
            print("No promotional emails found.")
        else:
            print(f"Found {count} promotional emails. Moving to Trash...")

            # Batch move to Trash using labels
            for i in range(0, count, 100):
                batch = mail_ids[i : i + 100]
                batch_str = b",".join(batch).decode()
                # Gmail specific move to trash
                mail.store(batch_str, "+X-GM-LABELS", "\\Trash")

            print(f"Successfully moved {count} emails to Trash.")

        mail.logout()
    except Exception as e:
        print(f"An error occurred: {e}")


if __name__ == "__main__":
    clean_promotions()
