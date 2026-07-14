import os
import argparse
import time
import re
import requests
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import markdownify
import warnings

# Suppress BeautifulSoup warnings
warnings.filterwarnings("ignore", category=UserWarning, module='bs4')

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name)

def get_headers():
    user_agent = os.getenv("SEC_USER_AGENT")
    if not user_agent:
        raise ValueError(".env 파일에 SEC_USER_AGENT가 설정되지 않았습니다.")
    return {'User-Agent': user_agent}

def get_cik_from_ticker(ticker):
    url = "https://www.sec.gov/files/company_tickers.json"
    headers = get_headers()
    response = requests.get(url, headers=headers)
    time.sleep(0.15) # SEC Rate limit (max 10 requests per second)
    if response.status_code != 200:
        raise ValueError(f"Ticker 목록 조회 실패: {response.status_code}")
    
    data = response.json()
    ticker_upper = ticker.upper()
    for key, value in data.items():
        if value['ticker'].upper() == ticker_upper:
            return str(value['cik_str']).zfill(10)
    
    return None

def process_filings_data(filings_data, dir_name, cik, headers, processed_years):
    """주어진 공시 데이터(딕셔너리)에서 10-K만 골라내어 다운로드하는 함수"""
    forms = filings_data.get("form", [])
    accession_numbers = filings_data.get("accessionNumber", [])
    report_dates = filings_data.get("reportDate", [])
    primary_documents = filings_data.get("primaryDocument", [])

    for i in range(len(forms)):
        # 최대 10년 치를 모두 모았다면 종료
        if len(processed_years) >= 10:
            break
            
        form = forms[i]
        
        # 10-K, 20-F, 40-F (정정공시 포함) 등 연례보고서 허용
        if not (form.startswith("10-K") or form.startswith("20-F") or form.startswith("40-F")):
            continue
            
        report_date = report_dates[i]
        year = report_date[:4] if report_date else "Unknown"
        
        # 이미 해당 연도의 보고서를 처리했다면 건너뜀 (가장 최신 10-K/A 또는 10-K를 우선)
        if year in processed_years:
            continue
            
        accession_no = accession_numbers[i]
        primary_doc = primary_documents[i]
        
        if not primary_doc:
            print(f"스킵: [{year}년] 메인 문서가 없습니다. (접수번호: {accession_no})")
            continue

        safe_form = form.replace("/", "-")
        filename = f"{year}_{safe_form}.md"
        filepath = os.path.join(dir_name, filename)
        
        if os.path.exists(filepath):
            print(f"콘솔 스킵 로그: 파일 이미 존재함 - [{year}년] {filename}")
            processed_years.add(year)
            continue
            
        print(f"[{year}년] 다운로드 시작: {form} (접수번호: {accession_no})")
        
        # Document URL 포맷팅
        acc_no_clean = accession_no.replace("-", "")
        cik_clean = str(int(cik)) # leading zeros 제거
        
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{acc_no_clean}/{primary_doc}"
        
        try:
            # 원문 HTML 다운로드
            doc_response = requests.get(doc_url, headers=headers)
            time.sleep(0.15)
            if doc_response.status_code != 200:
                print(f" -> 에러: 문서 다운로드 실패 (HTTP {doc_response.status_code})")
                continue
                
            html_text = doc_response.text
            
            # HTML 파싱 및 마크다운 변환
            soup = BeautifulSoup(html_text, 'html.parser')
            
            # 불필요한 iXBRL 메타데이터 및 숨김 태그 제거
            for ix_header in soup.find_all('ix:header'):
                ix_header.decompose()
            for hidden in soup.find_all(style=lambda value: value and 'display:none' in value.replace(' ', '').lower()):
                hidden.decompose()
                
            md_text = markdownify.markdownify(str(soup), heading_style="ATX", tables=True)
            
            # 파일로 저장
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(md_text)
                
            print(f" -> 성공: {filepath} 저장 완료.")
            processed_years.add(year)
        except Exception as e:
            print(f" -> 에러: {year}년 보고서 파싱 및 저장 실패 ({e})")
            continue


def main():
    parser = argparse.ArgumentParser(description="SEC EDGAR 10-K Downloader")
    parser.add_argument("--ticker", type=str, required=True, help="Stock ticker (e.g. BRK.B, AAPL)")
    args = parser.parse_args()
    
    ticker = args.ticker.upper()

    # 1. 환경 변수 및 헤더 로드
    load_dotenv()
    try:
        headers = get_headers()
    except ValueError as e:
        print(f"에러: {e}")
        print('.env 파일에 SEC_USER_AGENT="Your Name (email@example.com)" 형식으로 설정해주세요.')
        return

    # 2. Ticker -> CIK 매핑
    print(f"[{ticker}] CIK 정보 조회 중...")
    try:
        cik = get_cik_from_ticker(ticker)
        if not cik:
            print(f"에러: 종목코드 {ticker}에 해당하는 회사(CIK)를 찾을 수 없습니다.")
            return
    except Exception as e:
        print(f"CIK 조회 중 오류 발생: {e}")
        return

    print(f" -> CIK 확인: {cik}")

    # 3. 저장 폴더 생성
    dir_name = f"data/{sanitize_filename(ticker)}_{cik}"
    os.makedirs(dir_name, exist_ok=True)
    print(f"저장 폴더: {dir_name}")

    # 4. 최근 공시 목록(Submissions API) 조회
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        response = requests.get(submissions_url, headers=headers)
        time.sleep(0.15)
        if response.status_code != 200:
            print(f"보고서 목록 조회 실패: HTTP {response.status_code}")
            return
        submissions = response.json()
    except Exception as e:
        print(f"보고서 목록 조회 중 오류 발생: {e}")
        return
    
    processed_years = set()
    
    # 5. 최신 공시 내역(recent) 먼저 탐색
    recent_filings = submissions.get("filings", {}).get("recent", {})
    if recent_filings:
        process_filings_data(recent_filings, dir_name, cik, headers, processed_years)
    
    # 6. 최근 내역만으로 10년 치가 부족할 경우 과거 아카이브(files) 탐색
    older_files = submissions.get("filings", {}).get("files", [])
    for file_info in older_files:
        if len(processed_years) >= 10:
            break
            
        file_name = file_info.get("name")
        if not file_name:
            continue
            
        print(f"-> 10년 치 데이터를 채우기 위해 과거 공시 기록({file_name})을 추가 조회합니다...")
        older_url = f"https://data.sec.gov/submissions/{file_name}"
        
        try:
            older_response = requests.get(older_url, headers=headers)
            time.sleep(0.15)
            if older_response.status_code == 200:
                older_filings = older_response.json()
                process_filings_data(older_filings, dir_name, cik, headers, processed_years)
        except Exception as e:
            print(f" -> 과거 공시 기록 조회 실패: {e}")
                
    if not processed_years:
        print("조건에 맞는 연례보고서(10-K, 20-F 등)가 없습니다.")
    else:
        print(f"\n최종 완료! 총 {len(processed_years)}년 치의 연례보고서 다운로드 완료.")

if __name__ == "__main__":
    main()
