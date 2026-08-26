import os
import re
import sys
import random
import subprocess
import requests
import csv
import time
import traceback

BASE_API_URL = "https://sallyapi.witheldokan.com/api/customer/products/search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}

MAX_ITEMS = 10000
MIN_DELAY_SEC = 1.5
MAX_DELAY_SEC = 3.0
CSV_FILE = "phdrfhar.csv"
PROGRESS_FILE = "progress_ids.txt"
CHECKPOINT_EVERY = 1000
WORKFLOW_FILE = "daily_upnew.yml"


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_sections(html):
    if not html:
        return {}
    sections = {}
    matches = re.findall(r"<dt[^>]*>\s*<b>(.*?)</b>\s*</dt>(.*?)(?=<dt|\Z)", html, re.S)
    for label, body in matches:
        label_clean = strip_html(label).rstrip(":").strip()
        body_clean = strip_html(body)
        if label_clean and body_clean:
            sections[label_clean] = body_clean
    return sections


def find_section(sections, keyword):
    for label, text in sections.items():
        if keyword in label:
            return text
    return ""


def get_product_full_details(slug):
    result = {
        "active_ingredient": "N/A",
        "indications": "",
        "dosage": "",
    }
    detail_url = f"https://sallyapi.witheldokan.com/api/customer/products/{slug}/slug?ignore_similar_products=1"
    try:
        res = requests.get(detail_url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return result
        data = res.json()
        product = data.get("data", {}).get("product", {})

        for attr in product.get("details", {}).get("attributes", []):
            if attr.get("option_en") == "Active Ingredients":
                result["active_ingredient"] = attr.get("value_ar") or attr.get("value_en", "N/A")
                break

        desc_sections = parse_sections(product.get("description_ar", ""))
        result["indications"] = find_section(desc_sections, "الاستخدام")

        long_sections = parse_sections(product.get("long_description_ar", ""))
        result["dosage"] = find_section(long_sections, "الجرعة")

    except Exception:
        pass
    return result


def build_row(p):
    name_ar = p.get("name_ar", "N/A")
    name_en = p.get("name_en", "N/A")
    price = p.get("price", 0)
    slug = p.get("slug", "")

    brand_data = p.get("brand")
    brand_ar = brand_data.get("name_ar", "N/A") if isinstance(brand_data, dict) else "N/A"

    cat_data = p.get("category") or {}
    cat_ar = cat_data.get("name_ar", "N/A")

    sub_cats = cat_data.get("sub_categories") or []
    sub_cat_ar = sub_cats[0].get("name_ar", "N/A") if sub_cats else "N/A"

    special_label_ar = p.get("special_labels_ar", "") or ""
    requires_fridge = "ثلاج" in special_label_ar

    details = get_product_full_details(slug) if slug else {}
    time.sleep(random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC))

    return [
        name_ar, name_en, price,
        brand_ar, cat_ar, sub_cat_ar,
        details.get("active_ingredient", "N/A"),
        "Yes" if requires_fridge else "No",
        details.get("indications", ""),
        details.get("dosage", ""),
    ]


def load_progress():
    seen_ids = set()
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        seen_ids.add(int(line))
                    except ValueError:
                        continue
    return seen_ids


def git_checkpoint(item_count):
    try:
        subprocess.run(["git", "add", CSV_FILE, PROGRESS_FILE], check=True)
        commit = subprocess.run(
            ["git", "commit", "-m", f"Checkpoint: {item_count} items scraped"],
            capture_output=True, text=True
        )
        if commit.returncode == 0:
            push = subprocess.run(["git", "pull", "--rebase"], capture_output=True, text=True)
            push = subprocess.run(["git", "push"], capture_output=True, text=True)
            if push.returncode == 0:
                print(f"[Checkpoint] Committed and pushed at {item_count} items.")
            else:
                print(f"[Checkpoint] Push failed: {push.stderr.strip()}")
        else:
            print(f"[Checkpoint] Nothing new to commit ({commit.stdout.strip()} {commit.stderr.strip()})")
    except Exception as e:
        print(f"[Checkpoint] Git error: {e}")


def trigger_next_run():
    token = os.environ.get("GH_DISPATCH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    ref = os.environ.get("GITHUB_REF_NAME", "main")

    if not token or not repo:
        print("[Handoff] Missing GH_DISPATCH_TOKEN or GITHUB_REPOSITORY, cannot trigger next run.")
        return False

    url = f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        res = requests.post(url, headers=headers, json={"ref": ref}, timeout=15)
        if res.status_code in (204, 201):
            print("[Handoff] Successfully triggered a new workflow run to continue.")
            return True
        else:
            print(f"[Handoff] Failed to trigger next run: {res.status_code} {res.text}")
            return False
    except Exception as e:
        print(f"[Handoff] Error triggering next run: {e}")
        return False


def run_scraper():
    seen_ids = load_progress()
    resuming = len(seen_ids) > 0
    write_header = not (resuming and os.path.exists(CSV_FILE))
    file_mode = "a" if resuming and os.path.exists(CSV_FILE) else "w"

    if resuming:
        print(f"Resuming previous run. {len(seen_ids)} items already scraped.")
    else:
        print(f"Starting fresh scrape (max {MAX_ITEMS} items). Output file: {CSV_FILE}")

    total_items = len(seen_ids)
    batch_start_count = total_items

    csv_file = open(CSV_FILE, mode=file_mode, newline="", encoding="utf-8-sig")
    progress_file = open(PROGRESS_FILE, mode="a")
    writer = csv.writer(csv_file)

    if write_header:
        writer.writerow([
            "Name_AR", "Name_EN", "Price",
            "Brand_AR", "Category_AR", "Sub_Category_AR",
            "Active_Ingredient", "Requires_Fridge",
            "Indications", "Dosage"
        ])
        csv_file.flush()

    page_num = 1
    start_time = time.time()
    consecutive_duplicate_pages = 0
    MAX_CONSECUTIVE_DUPLICATE_PAGES = 3
    MAX_PAGES_SAFETY = 3000
    catalog_finished = False

    while total_items < MAX_ITEMS and page_num <= MAX_PAGES_SAFETY:
        params = {"enable_search_side_filters": "1", "page": page_num}
        try:
            response = requests.get(BASE_API_URL, headers=HEADERS, params=params, timeout=25)
            print(f"Page {page_num} -> HTTP status: {response.status_code}")

            if response.status_code != 200:
                print("Non-200 status, stopping this run.")
                break

            data = response.json()
            products = data.get("data", {}).get("products", [])
            print(f"Page {page_num} -> products returned: {len(products)}")

            if not products:
                print("No products on this page. Catalog finished.")
                catalog_finished = True
                break

            new_products = [p for p in products if p.get("id") not in seen_ids]

            if not new_products:
                consecutive_duplicate_pages += 1
                print(f"All products on this page already seen "
                      f"({consecutive_duplicate_pages}/{MAX_CONSECUTIVE_DUPLICATE_PAGES} consecutive duplicate pages).")
                if consecutive_duplicate_pages >= MAX_CONSECUTIVE_DUPLICATE_PAGES:
                    print("Confirmed end of catalog. Catalog finished.")
                    catalog_finished = True
                    break
                page_num += 1
                continue
            else:
                consecutive_duplicate_pages = 0

            for p in products:
                seen_ids.add(p.get("id"))

            for p in new_products:
                if total_items >= MAX_ITEMS:
                    break
                try:
                    row = build_row(p)
                    writer.writerow(row)
                    csv_file.flush()
                    progress_file.write(f"{p.get('id')}\n")
                    progress_file.flush()
                    total_items += 1
                    elapsed = time.time() - start_time
                    print(f"[{total_items}/{MAX_ITEMS}] {row[1]} (elapsed: {elapsed:.1f}s)")

                    if total_items - batch_start_count >= CHECKPOINT_EVERY:
                        csv_file.flush()
                        progress_file.flush()
                        csv_file.close()
                        progress_file.close()
                        git_checkpoint(total_items)
                        triggered = trigger_next_run()
                        print(f"\nBatch of {CHECKPOINT_EVERY} done. Total so far: {total_items}. "
                              f"Handoff triggered: {triggered}. Ending this run.")
                        sys.exit(0)

                except Exception:
                    print(f"[Row error] id={p.get('id')} name={p.get('name_en')}")
                    traceback.print_exc()
                    continue

            page_num += 1

        except requests.exceptions.RequestException as e:
            print(f"[Connection issue] {e} - retrying in 5s")
            time.sleep(5)
            continue

    csv_file.close()
    progress_file.close()

    git_checkpoint(total_items)

    total_time = time.time() - start_time
    if catalog_finished or total_items >= MAX_ITEMS:
        print(f"\nAll done. Full catalog scraped. Total items: {total_items}. This run took {total_time:.1f} seconds.")
    else:
        print(f"\nRun ended (page safety limit or error). Total items so far: {total_items}. "
              f"This run took {total_time:.1f} seconds.")


if __name__ == "__main__":
    run_scraper()
