import time
import re
import json
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from oauth2client.service_account import ServiceAccountCredentials
import gspread

# Google Sheets設定
SERVICE_ACCOUNT_FILE = "credentials.json"
SHEET_NAME = "ニュース収集シート"

# Selenium設定
def init_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    service = Service("/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=chrome_options)
    print("[DEBUG] WebDriver initialized.")
    return driver

# 本文抽出（1ページ分）
def extract_body(soup):
    article = soup.find("article") or soup.find("div", class_=re.compile(r"(articleBody|ArticleBody)"))
    if not article:
        print("[DEBUG] No article container found.")
        return ""

    for tag in article.find_all(["figure", "aside", "script", "style", "noscript"]):
        tag.decompose()

    paragraphs = [p.get_text(" ", strip=True) for p in article.find_all("p") if p.get_text(strip=True)]
    body = "\n".join(paragraphs)
    print(f"[DEBUG] Extracted body part length: {len(body)}")
    return body

# 複数ページ対応
def extract_full_body(driver, base_url):
    full_text = ""
    for page in range(1, 6):  # 最大5ページまで試す
        page_url = f"{base_url}?page={page}" if page > 1 else base_url
        print(f"[DEBUG] Loading page: {page_url}")
        driver.get(page_url)
        time.sleep(2)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        part = extract_body(soup)
        if not part:
            print(f"[DEBUG] No content found on page {page}, stopping.")
            break
        full_text += part + "\n"
    print(f"[DEBUG] Extracted full body length: {len(full_text)}")
    return full_text.strip()

# 記事情報取得
def extract_article_info(driver, url):
    try:
        print(f"[DEBUG] Extracting info from: {url}")
        driver.get(url)
        time.sleep(2)
        soup = BeautifulSoup(driver.page_source, "html.parser")

        meta_title = soup.find("meta", property="og:title")
        title = meta_title["content"].strip() if meta_title and meta_title.get("content") else "NO TITLE"
        print(f"[DEBUG] Title: {title}")

        provider = "Unknown"
        meta_author = soup.find("meta", attrs={"name": re.compile("author|publisher", re.I)})
        if meta_author and meta_author.get("content"):
            provider = meta_author["content"].strip()
        else:
            ld_json = soup.find("script", type="application/ld+json")
            if ld_json:
                try:
                    data = json.loads(ld_json.string)
                    if isinstance(data, dict):
                        provider = data.get("author", {}).get("name", provider)
                except Exception as e:
                    print(f"[DEBUG] Failed to parse ld+json: {e}")
        
        genre = "国内" # Default genre if not found

        # Extract genre from __PRELOADED_STATE__
        # Use regex to precisely capture the JSON object
        preloaded_state_match = re.search(r'window\.__PRELOADED_STATE__ = (\{.*?\});', driver.page_source, re.DOTALL)
        
        if preloaded_state_match:
            json_str = preloaded_state_match.group(1)
            try:
                state_data = json.loads(json_str)
                print(f"[DEBUG] Raw __PRELOADED_STATE__ data: {state_data}")
                
                # Attempt to get category and subcategory from articleDetail first
                category_short_name = state_data.get('articleDetail', {}).get('categoryShortName')
                sub_category = state_data.get('articleDetail', {}).get('subCategory')

                # If not found in articleDetail, try pageData.pageParam.cat_path
                if not (category_short_name and sub_category):
                    cat_path = state_data.get('pageData', {}).get('pageParam', {}).get('cat_path')
                    if cat_path:
                        path_parts = cat_path.split(',')
                        if len(path_parts) >= 2 and path_parts[0] and path_parts[1]:
                            category_short_name = path_parts[0]
                            sub_category = path_parts[1]
                        elif len(path_parts) == 1 and path_parts[0]:
                            category_short_name = path_parts[0]
                            sub_category = None # Ensure sub_category is None if only one part

                print(f"[DEBUG] Resolved category_short_name: {category_short_name}, type: {type(category_short_name)}")
                print(f"[DEBUG] Resolved sub_category: {sub_category}, type: {type(sub_category)}")

                # Construct genre string if valid parts are found
                if category_short_name and isinstance(category_short_name, str) and len(category_short_name) > 0:
                    category_cap = category_short_name.capitalize()
                    if sub_category and isinstance(sub_category, str) and len(sub_category) > 0:
                        sub_cap = sub_category.capitalize()
                        genre = f"{category_cap}/{sub_cap}"
                    else:
                        genre = category_cap
                
                print(f"[DEBUG] Extracted genre from JSON: {genre}")
            except json.JSONDecodeError as e:
                print(f"[DEBUG] Failed to parse __PRELOADED_STATE__ JSON (regex method): {e}")
            except Exception as e:
                print(f"[DEBUG] An unexpected error occurred while processing __PRELOADED_STATE__ (regex method): {e}")
        else:
            print("[DEBUG] __PRELOADED_STATE__ script not found or regex failed to match.")

        pub_time = soup.find("time").get_text(strip=True) if soup.find("time") else ""
        body = extract_full_body(driver, url)

        print(f"[DEBUG] Final genre before return: {genre}")
        return title, provider, pub_time, body[:3000] if body else "", genre
    except Exception as e:
        print(f"[ERROR] Failed to extract article info from {url}: {e}")
        return "ERROR", "ERROR", "", "", "Unknown" # Return "Unknown" for genre in case of error

# スプレッドシートへ書き込み
def append_to_sheet(data, existing_urls):
    print(f"[INFO] Writing {len(data)} new records to the sheet...")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
    client = gspread.authorize(creds)
    sheet = client.open(SHEET_NAME).sheet1

    existing = sheet.get_all_values()
    if not existing:
        headers = ["ID", "収集時刻", "タイトル", "情報源", "掲載時刻", "URL", "ジャンル", "本文"]
        sheet.append_row(headers)
        print("[INFO] Header row inserted.")

    # 新規URLフィルタリング
    new_rows = [row for row in data if row[5] not in existing_urls]
    print(f"[INFO] {len(new_rows)} new unique records to write.")

    if new_rows:
        # 一括書き込み
        sheet.append_rows(new_rows, value_input_option="RAW")
        print(f"[INFO] Completed. {len(new_rows)} rows added.")
    else:
        print("[INFO] No new records to write.")


# メイン処理
if __name__ == "__main__":
    print("[START] Yahoo News scraping started.")
    driver = init_driver()
    driver.get("https://news.yahoo.co.jp/categories/domestic")

    for i in range(5):
        try:
            more_button = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(),'もっと見る')]"))
            )
            driver.execute_script("arguments[0].click();", more_button)
            print(f"[DEBUG] Clicked 'もっと見る' button {i+1}回目")
            time.sleep(2)
        except Exception:
            print("[INFO] No more 'もっと見る' button or reached limit.")
            break

    soup = BeautifulSoup(driver.page_source, "html.parser")

    jst = timezone(timedelta(hours=9))
    timestamp = datetime.now(jst).strftime("%Y/%m/%d %H:%M")
    today_str = datetime.now(jst).strftime("%Y/%m/%d")

    articles = soup.select("a[href^='https://news.yahoo.co.jp/articles/']")
    print(f"[DEBUG] Found {len(articles)} article links.")
    seen_urls = set()
    data = []
    counter = 1
    skipped = 0

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
    client = gspread.authorize(creds)
    sheet = client.open(SHEET_NAME).sheet1
    existing_urls = [row[5] for row in sheet.get_all_values()[1:] if len(row) > 5]
    print(f"[INFO] Fetched {len(existing_urls)} existing URLs from the sheet.")

    for a in articles:
        article_url = a["href"].split("?")[0]
        if article_url in seen_urls or article_url in existing_urls:
            print(f"[SKIP] Already exists: {article_url}")
            skipped += 1
            continue
        seen_urls.add(article_url)

        title, provider, pub_time, body, genre = extract_article_info(driver, article_url)
        print(f"[DEBUG] Body head: {body[:80]}\n")
        if not body or not title:
            print(f"[SKIP] Invalid content from: {article_url}")
            skipped += 1
            continue

        print(f"[ADD] {title} ({article_url}) - Genre: {genre}")
        data.append([
            f"{today_str} {counter}",
            timestamp,
            title,
            provider,
            pub_time,
            article_url,
            genre, # Use the extracted genre
            body
        ])
        counter += 1

    driver.quit()

    if data:
        append_to_sheet(data, existing_urls)
    else:
        print("[INFO] No new articles to write.")

    print(f"[REPORT] Skipped: {skipped}, Added: {len(data)}")
