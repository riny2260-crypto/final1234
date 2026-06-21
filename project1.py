import streamlit as st
import fitz  # PyMuPDF (PDF 분석용 도구)
import os
import re
import json
import pandas as pd
from datetime import datetime

# 구글 드라이브 클라우드 소통용 특수 도구들
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

# =========================================================================
# [설정 사항] 우리 학교 환경에 맞게 이름과 키워드만 관리하는 구역입니다.
# =========================================================================

# 1. 우리 학교 전체 선생님 명단
ALL_TEACHERS = ["김철수", "이영희", "박민수", "최수연", "정우성", "홍길동", "조서린"]

# 2. 인식할 연수명 힌트 단어(키워드) 주머니
TRAINING_KEYWORDS = {
    "다문화이해교육": ["다문화", "상호문화", "다문화이해"],
    "성희롱예방교육": ["성희롱", "폭력예방", "양성평등", "4대폭력"],
    "안전보건교육": ["안전보건", "산업안전", "중대재해"],
    "학교폭력예방교육": ["학교폭력", "학폭예방"],
    "아동학대예방교육": ["아동학대", "학대신고"],
    "개인정보보호교육": ["개인정보", "정보보안"],
    "청렴교육": ["부패방지", "청렴", "이해충돌"],
    "긴급복지신고의무자교육": ["긴급복지", "긴급", "신고의무자"]
}

# 3. 구글 드라이브 클라우드 접근 권한 주소 (읽기/쓰기 전체 권한)
SCOPES = ['https://www.googleapis.com/auth/drive']


# =========================================================================


# 🔑 [구글 클라우드 로그인 인증 마법 함수 - 배포 환경 지원 버전]
def get_gdrive_service():
    creds = None

    # [루트 1] 내 컴퓨터(로컬)에서 실행할 때: 파일이 존재하면 가져오기
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())

    # [루트 2] Streamlit Cloud 웹 배포 환경일 때: Secrets 시스템에서 가져오기
    elif "gdrive" in st.secrets:
        try:
            # Secrets 창에 등록해 둔 token_json 문장(스트링)을 딕셔너리로 변환
            token_info = json.loads(st.secrets["gdrive"]["token_json"])
            creds = Credentials.from_authorized_user_info(token_info, SCOPES)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
        except Exception as e:
            st.error(f"Secrets 인증키 로드 실패: {e}")

    # [루트 3] 둘 다 열쇠가 없거나 최초 실행일 때 (로컬 전용 새 로그인 브라우저 오픈)
    if not creds:
        if os.path.exists('client_secret.json'):
            flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
            creds = flow.run_local_server(port=0)
            with open('token.json', 'w') as token:
                token.write(creds.to_json())
        else:
            raise FileNotFoundError("구글 드라이브 인증 정보(token.json 또는 Secrets)를 찾을 수 없습니다.")

    return build('drive', 'v3', credentials=creds)


# 📂 [구글 클라우드 내부에 폴더가 있으면 ID를 찾고, 없으면 새로 만드는 함수]
def get_or_create_drive_folder(service, folder_name, parent_id=None):
    query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"

    results = service.files().list(q=query, fields="files(id)").execute()
    items = results.get('files', [])

    if items:
        return items[0]['id']
    else:
        file_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        if parent_id:
            file_metadata['parents'] = [parent_id]
        folder = service.files().create(body=file_metadata, fields='id').execute()
        return folder.get('id')


# 🔍 [동일 이름의 기존 PDF 파일이 있는지 구글 드라이브 검색하는 함수]
def find_existing_file(service, filename, folder_id):
    query = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id)").execute()
    items = results.get('files', [])
    return items[0]['id'] if items else None


# 🔍 [PDF 상세 정보 분석 함수]
def analyze_pdf_details(file_bytes):
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    full_text = "".join([page.get_text() for page in doc])

    # 1) 성함 추출
    detected_name = "미확인이름"
    for name in ALL_TEACHERS:
        if name in full_text:
            detected_name = name
            break

    # 2) 여러 연수명 추출 (중복 매칭 허용)
    detected_courses = []
    for course_name, keywords in TRAINING_KEYWORDS.items():
        if any(keyword in full_text for keyword in keywords):
            detected_courses.append(course_name)
    if not detected_courses:
        detected_courses.append("기타연수")

    # 3) 이수번호 추출
    serial_match = re.search(r'(제\s*[\w\s-]+