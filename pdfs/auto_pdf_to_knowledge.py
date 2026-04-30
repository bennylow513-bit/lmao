import os
import time
from datetime import datetime
from pathlib import Path

import fitz  # pymupdf
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


PDF_FOLDER = Path("pdfs")
KNOWLEDGE_FILE = Path("knowledge.txt")
PROCESSED_FILE = Path("processed_pdfs.txt")


def load_processed_pdfs() -> set:
    if not PROCESSED_FILE.exists():
        return set()

    with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_processed_pdf(filename: str) -> None:
    with open(PROCESSED_FILE, "a", encoding="utf-8") as f:
        f.write(filename + "\n")


def extract_text_from_pdf(pdf_path: Path) -> str:
    text_parts = []

    try:
        doc = fitz.open(pdf_path)

        for page_number, page in enumerate(doc, start=1):
            text = page.get_text("text").strip()

            if text:
                text_parts.append(f"\n--- Page {page_number} ---\n{text}")

        doc.close()

    except Exception as e:
        print(f"Failed to read PDF {pdf_path.name}: {e}")
        return ""

    return "\n".join(text_parts).strip()


def clean_text(text: str) -> str:
    lines = []

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        lines.append(line)

    return "\n".join(lines)


def append_pdf_to_knowledge(pdf_path: Path) -> None:
    processed = load_processed_pdfs()

    if pdf_path.name in processed:
        print(f"Already processed: {pdf_path.name}")
        return

    print(f"Processing PDF: {pdf_path.name}")

    raw_text = extract_text_from_pdf(pdf_path)

    if not raw_text:
        print(f"No text found in {pdf_path.name}. It may be a scanned image PDF.")
        return

    cleaned_text = clean_text(raw_text)

    today = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    section = f"""

==================================================
AUTO-IMPORTED PDF: {pdf_path.name}
Imported on: {today}
==================================================

Important:
- This section was auto-extracted from a PDF.
- Check and clean this section before final submission.
- If any information conflicts with earlier confirmed knowledge, update the correct section manually.
- Do not treat unclear or incomplete information as confirmed.

{cleaned_text}

==================================================
END OF AUTO-IMPORTED PDF: {pdf_path.name}
==================================================

"""

    with open(KNOWLEDGE_FILE, "a", encoding="utf-8") as f:
        f.write(section)

    save_processed_pdf(pdf_path.name)

    print(f"Added {pdf_path.name} to knowledge.txt")


class PDFHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return

        path = Path(event.src_path)

        if path.suffix.lower() == ".pdf":
            # Wait a few seconds so VS Code finishes copying the file
            time.sleep(3)
            append_pdf_to_knowledge(path)


def process_existing_pdfs():
    PDF_FOLDER.mkdir(exist_ok=True)

    for pdf_path in PDF_FOLDER.glob("*.pdf"):
        append_pdf_to_knowledge(pdf_path)


def watch_pdf_folder():
    PDF_FOLDER.mkdir(exist_ok=True)

    print("Watching pdfs/ folder...")
    print("Drop a PDF into the pdfs folder and it will be added to knowledge.txt.")
    print("Press CTRL + C to stop.")

    process_existing_pdfs()

    event_handler = PDFHandler()
    observer = Observer()
    observer.schedule(event_handler, str(PDF_FOLDER), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        observer.stop()

    observer.join()


if __name__ == "__main__":
    watch_pdf_folder()