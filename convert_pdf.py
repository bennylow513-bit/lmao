from pathlib import Path
from pypdf import PdfReader

PDF_PATH = Path("data/jal_yoga_faq.pdf")
TXT_PATH = Path("data/jal_yoga_faq.txt")


def main() -> None:
    if not PDF_PATH.exists():
        raise FileNotFoundError(f"PDF not found: {PDF_PATH}")

    reader = PdfReader(str(PDF_PATH))
    pages = []

    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        text = text.strip()
        pages.append(f"===== PAGE {page_number} =====\n{text}\n")

    final_text = "\n".join(pages).strip()
    TXT_PATH.write_text(final_text, encoding="utf-8")

    print(f"Done. Text file created at: {TXT_PATH}")


if __name__ == "__main__":
    main()