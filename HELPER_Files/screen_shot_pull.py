from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont
import os

OUTPUT_DIR = "annotated_screenshots"
os.makedirs(OUTPUT_DIR, exist_ok=True)


def annotate(image_path, boxes_with_labels, output_path):
    img = Image.open(image_path)
    draw = ImageDraw.Draw(img)

    for box, label, color in boxes_with_labels:
        x, y, w, h = box["x"], box["y"], box["width"], box["height"]

        # Draw rectangle
        draw.rectangle(
            [(x, y), (x + w, y + h)],
            outline=color,
            width=3
        )

        # Add label text
        draw.text((x, y - 20), label, fill=color)

    img.save(output_path)


def textbox_invalid(page):
    page.goto("https://demoqa.com/text-box")

    page.fill("#userEmail", "test@domain")
    page.click("#submit")

    raw_path = f"{OUTPUT_DIR}/textbox_invalid_raw.png"
    final_path = f"{OUTPUT_DIR}/textbox_invalid_annotated.png"

    page.screenshot(path=raw_path, full_page=True)

    email_box = page.locator("#userEmail").bounding_box()

    annotate(
        raw_path,
        [
            (email_box, "Invalid email → validation triggered", "red"),
        ],
        final_path
    )


def textbox_valid(page):
    page.goto("https://demoqa.com/text-box")

    page.fill("#userEmail", "test@example.com")
    page.fill("#userName", "Test User")
    page.click("#submit")

    raw_path = f"{OUTPUT_DIR}/textbox_valid_raw.png"
    final_path = f"{OUTPUT_DIR}/textbox_valid_annotated.png"

    page.screenshot(path=raw_path, full_page=True)

    output_box = page.locator("#output").bounding_box()

    annotate(
        raw_path,
        [
            (output_box, "Output rendered on valid input", "green"),
        ],
        final_path
    )


def webtables_invalid(page):
    page.goto("https://demoqa.com/webtables")

    page.click("#addNewRecordButton")
    page.fill("#firstName", "Test")
    page.fill("#lastName", "User")
    page.fill("#userEmail", "test@test.com")
    page.fill("#age", "abc")
    page.fill("#salary", "1000")
    page.fill("#department", "QA")

    page.click("#submit")

    raw_path = f"{OUTPUT_DIR}/webtables_invalid_raw.png"
    final_path = f"{OUTPUT_DIR}/webtables_invalid_annotated.png"

    page.screenshot(path=raw_path, full_page=True)

    age_box = page.locator("#age").bounding_box()

    annotate(
        raw_path,
        [
            (age_box, "Invalid numeric input → submission blocked", "red"),
        ],
        final_path
    )


def radio_disabled(page):
    page.goto("https://demoqa.com/radio-button")

    raw_path = f"{OUTPUT_DIR}/radio_raw.png"
    final_path = f"{OUTPUT_DIR}/radio_annotated.png"

    page.screenshot(path=raw_path, full_page=True)

    no_radio = page.locator("#noRadio").bounding_box()

    annotate(
        raw_path,
        [
            (no_radio, "Disabled option (#noRadio)", "red"),
        ],
        final_path
    )


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        textbox_invalid(page)
        textbox_valid(page)
        webtables_invalid(page)
        radio_disabled(page)

        browser.close()


if __name__ == "__main__":
    run()