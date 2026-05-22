import os
import re
import io
import pandas as pd
import streamlit as st
import plotly.express as px
from datetime import datetime, timedelta, timezone
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ============================================================
# 기본 설정
# ============================================================
KST = timezone(timedelta(hours=9))

def get_now():
    return datetime.now(KST)

st.set_page_config(
    page_title="계약TF vs 법무팀 업무량 비교",
    layout="wide"
)

TARGET_KEYWORD = "김정민"
GROUP_TF = "계약TF"
GROUP_LEGAL = "법무팀"
CUMULATIVE_START_DATE = datetime(2026, 5, 19)

# ------------------------------------------------------------
# 상태 기준
# ------------------------------------------------------------
ACTIVE_STATUSES = ["법무 검토 중"]

DONE_STATUSES = [
    "법무 승인 중", "법무 확인 완료",
    "결재 생략", "결재생략",
    "서명 생략", "서명생략",
    "서명 진행 중", "서명진행중",
    "종료(체결)", "종료 (체결)",
    "종료(미체결)", "종료 (미체결)",
]

EXCLUDE_STATUSES = ["의뢰 중", "의뢰중", "의뢰 반려", "의뢰반려"]

STATUS_REPLACE_MAP = {
    "법무 확인완료": "법무 확인 완료",
    "법무확인완료": "법무 확인 완료",
    "법무 검토중": "법무 검토 중",
    "법무검토중": "법무 검토 중",
    "법무 승인중": "법무 승인 중",
    "법무승인중": "법무 승인 중",
    "의뢰중": "의뢰 중",
    "의뢰반려": "의뢰 반려",
    "결재생략": "결재 생략",
    "서명생략": "서명 생략",
    "서명진행중": "서명 진행 중",
}

APPROVAL_COLS = [
    "검토의뢰 승인일시", "검토 의뢰 승인 일시",
    "검토의뢰 승인 일시", "검토 의뢰 승인일시",
]
LAW_REPLY_COLS = ["법무 검토 회신일", "법무검토 회신일", "법무검토회신일"]
REVIEWER_COLS  = ["검토자", "실제 검토자"]
UPDATE_COLS    = ["업데이트", "업데이트일", "업데이트 일시"]
DATE_COLS = [
    "의뢰일",
    "검토의뢰 승인일시", "검토 의뢰 승인 일시",
    "검토의뢰 승인 일시", "검토 의뢰 승인일시",
    "법무 검토 회신일", "법무검토 회신일", "법무검토회신일",
    "최종 검토 회신일",
    "업데이트", "업데이트일", "업데이트 일시",
]

# ============================================================
# 유틸 함수 (기존과 동일)
# ============================================================
def normalize_text(x):
    if pd.isna(x):
        return ""
    return str(x).strip()

def normalize_status(x):
    value = normalize_text(x)
    return STATUS_REPLACE_MAP.get(value, value)

def has_korean_name(name):
    n = normalize_text(name)
    if not n:
        return False
    if n == "⚠️ 미배정":
        return False
    if "삭제됨" in n:
        return False
    return bool(re.search("[가-힣]", n))

def is_blank_series(series):
    return series.isna() | (series.astype(str).str.strip() == "")

def find_col(df, candidates):
    for col in candidates:
        if col in df.columns:
            return col
    return None

# ============================================================
# Google Drive 데이터 로드 (Colab drive.mount 대체)
# ============================================================
@st.cache_data(ttl=300)  # 5분 캐시. 새로고침 버튼으로 즉시 갱신 가능
def load_data():
    # ① Service Account로 Drive 인증
    credentials = service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    service = build("drive", "v3", credentials=credentials)
    folder_id = st.secrets["drive"]["folder_id"]

    # ② 폴더에서 가장 최근 xlsx/csv 파일 검색
    query = (
        f"'{folder_id}' in parents "
        f"and (name contains '.xlsx' or name contains '.csv') "
        f"and trashed = false"
    )
    results = service.files().list(
        q=query,
        orderBy="modifiedTime desc",
        pageSize=1,
        fields="files(id, name, modifiedTime)"
    ).execute()

    files = results.get("files", [])
    if not files:
        st.error("Google Drive 폴더에 xlsx 또는 csv 파일이 없습니다. 파일을 업로드해 주세요.")
        st.stop()

    latest = files[0]

    # ③ 파일 다운로드 (메모리에)
    request = service.files().get_media(fileId=latest["id"])
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)

    # ④ 파일 읽기
    if latest["name"].endswith(".xlsx"):
        df = pd.read_excel(fh)
    else:
        try:
            df = pd.read_csv(fh, encoding="utf-8-sig")
        except UnicodeDecodeError:
            fh.seek(0)
            df = pd.read_csv(fh, encoding="cp949")

    # ⑤ 이하 기존 load_data 로직과 동일
    df.columns = [str(c).strip() for c in df.columns]

    required_cols = ["계약 상태", "배정된 검토자", "최종 검토 회신일", "계약명"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        st.error(f"필수 컬럼이 없습니다: {missing}")
        st.stop()

    approval_col = find_col(df, APPROVAL_COLS)
    law_reply_col = find_col(df, LAW_REPLY_COLS)
    reviewer_col  = find_col(df, REVIEWER_COLS)
    update_col    = find_col(df, UPDATE_COLS)

    for col_var, label in [
        (approval_col, "검토의뢰 승인일시"),
        (law_reply_col, "법무 검토 회신일"),
        (reviewer_col, "검토자"),
        (update_col, "업데이트"),
    ]:
        if col_var is None:
            st.error(f"'{label}' 컬럼을 찾지 못했습니다.")
            st.stop()

    df["계약 상태"]    = df["계약 상태"].apply(normalize_status)
    df["배정된 검토자"] = df["배정된 검토자"].fillna("⚠️ 미배정").apply(normalize_text)
    df = df[df["배정된 검토자"].apply(has_korean_name)].copy()

    for col in DATE_COLS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    df["_검토의뢰승인일시"] = pd.to_datetime(df[approval_col],  errors="coerce")
    df["_법무검토회신일"]   = pd.to_datetime(df[law_reply_col], errors="coerce")
    df["_업데이트"]        = pd.to_datetime(df[update_col],     errors="coerce")
    df["_검토자값"]        = df[reviewer_col]
    df["_업무그룹"]        = df["배정된 검토자"].apply(
        lambda x: GROUP_TF if TARGET_KEYWORD in str(x) else GROUP_LEGAL
    )

    return df, latest["name"], latest["modifiedTime"][:10]

# ============================================================
# 집계 함수 (기존과 동일)
# ============================================================
def get_today_new_assignment_condition(g, today):
    case_1 = (
        g["_업데이트"].notna()
        & (g["_업데이트"].dt.date == today)
        & g["배정된 검토자"].notna()
        & (g["배정된 검토자"].astype(str).str.strip() != "")
        & is_blank_series(g["_검토자값"])
    )
    case_2 = (
        g["_검토의뢰승인일시"].notna()
        & (g["_검토의뢰승인일시"].dt.date == today)
        & g["_법무검토회신일"].notna()
        & (g["_법무검토회신일"].dt.date == today)
    )
    return case_1 | case_2

def count_metrics(df, group, base_dt):
    today = base_dt.date()
    g = df[df["_업무그룹"] == group].copy()
    return {
        "현재 검토중": len(g[g["계약 상태"].isin(ACTIVE_STATUSES)]),
        "당일 신규 배정건": len(g[get_today_new_assignment_condition(g, today)]),
        "금일 처리 건수": len(
            g[g["계약 상태"].isin(DONE_STATUSES)
              & g["최종 검토 회신일"].notna()
              & (g["최종 검토 회신일"].dt.date == today)]
        ),
        "누적 처리 건수(5.19 이후)": len(
            g[g["계약 상태"].isin(DONE_STATUSES)
              & g["최종 검토 회신일"].notna()
              & (g["최종 검토 회신일"] >= CUMULATIVE_START_DATE)]
        ),
    }

def get_detail(df, group, metric, base_dt):
    today = base_dt.date()
    g = df[df["_업무그룹"] == group].copy()

    if metric == "현재 검토중":
        result = g[g["계약 상태"].isin(ACTIVE_STATUSES)]
    elif metric == "당일 신규 배정건":
        result = g[get_today_new_assignment_condition(g, today)]
    elif metric == "금일 처리 건수":
        result = g[
            g["계약 상태"].isin(DONE_STATUSES)
            & g["최종 검토 회신일"].notna()
            & (g["최종 검토 회신일"].dt.date == today)
        ]
    elif metric == "누적 처리 건수(5.19 이후)":
        result = g[
            g["계약 상태"].isin(DONE_STATUSES)
            & g["최종 검토 회신일"].notna()
            & (g["최종 검토 회신일"] >= CUMULATIVE_START_DATE)
        ]
    else:
        result = g.iloc[0:0]

    cols = [
        "의뢰일", "검토의뢰 승인일시", "검토 의뢰 승인 일시", "업데이트",
        "법무 검토 회신일", "최종 검토 회신일", "의뢰부서", "계약명",
        "계약 분류", "계약 상태", "배정된 검토자", "검토자",
    ]
    cols = [c for c in cols if c in result.columns]
    return result[cols].head(300)

# ============================================================
# UI (기존과 동일)
# ============================================================
df, file_name, file_date = load_data()
now     = get_now()
base_dt = now.replace(tzinfo=None)

# 헤더 (좌: 대웅 로고 / 중앙: 제목 / 우: 법무팀 이미지)
col_logo_left, col_title, col_logo_right = st.columns([1, 5, 1])

with col_logo_left:
    if os.path.exists("dw_logo.png"):
        st.image("dw_logo.png", width=120)

with col_title:
    st.title("계약TF vs 법무팀 업무량 비교")
    st.caption(
        f"집계 시각: {now.strftime('%Y-%m-%d %H:%M')} KST  |  "
        f"데이터 파일: {file_name} ({file_date})"
    )

with col_logo_right:
    if os.path.exists("legal_team.png"):
        st.image("legal_team.png", width=120)

# 새로고침 버튼 (파일 업로드 후 즉시 반영)
if st.button("🔄 데이터 새로고침"):
    load_data.clear()
    st.rerun()

st.divider()

tf    = count_metrics(df, GROUP_TF,    base_dt)
legal = count_metrics(df, GROUP_LEGAL, base_dt)
metrics = ["현재 검토중", "당일 신규 배정건", "금일 처리 건수", "누적 처리 건수(5.19 이후)"]

# 핵심 지표
st.subheader("업무량 핵심 지표")
row = st.columns(4)
for col, metric in zip(row, metrics):
    with col:
        st.markdown(f"### {metric}")
        c1, c2 = st.columns(2)
        c1.metric(GROUP_TF,    f"{tf[metric]}건")
        c2.metric(GROUP_LEGAL, f"{legal[metric]}건")

# 그래프
st.divider()
st.subheader("지표별 총량 비교")
chart_data = []
for metric in metrics:
    chart_data.append({"항목": metric, "구분": GROUP_TF,    "건수": tf[metric]})
    chart_data.append({"항목": metric, "구분": GROUP_LEGAL, "건수": legal[metric]})

fig = px.bar(
    pd.DataFrame(chart_data),
    x="항목", y="건수", color="구분", barmode="group", text="건수",
    color_discrete_map={GROUP_TF: "#FF6B00", GROUP_LEGAL: "#333333"}
)
fig.update_layout(height=430, xaxis_title="", yaxis_title="건수", legend_title_text="")
fig.update_traces(textposition="outside")
st.plotly_chart(fig, use_container_width=True)

# 상세 목록
st.divider()
st.subheader("상세 목록")
detail_col1, detail_col2 = st.columns([1, 2])
with detail_col1:
    selected_group  = st.radio("구분", [GROUP_TF, GROUP_LEGAL], horizontal=True)
with detail_col2:
    selected_metric = st.selectbox("항목", metrics)

detail_df = get_detail(df, selected_group, selected_metric, base_dt)
st.info(f"{selected_group} / {selected_metric}: {len(detail_df)}건")
st.dataframe(detail_df, use_container_width=True, hide_index=True)

st.divider()
st.caption("""
    참고:
    - 본 대시보드는 계약TF와 법무팀의 국내 계약 검토 업무량 비교를 위한 자료입니다.
    - 인도네시아 등 해외 법인 계약 검토 건은 제외했습니다.
    - 당일 신규 배정건은 당일 배정 또는 당일 접수 후 즉시 처리된 건을 포함합니다.
    - 처리 건수는 법무 검토가 완료된 건을 기준으로 집계했습니다.
""")
