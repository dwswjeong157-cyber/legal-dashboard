import os
import re
import io
import smtplib
import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.io as pio
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from datetime import datetime, timedelta, timezone
from dateutil.relativedelta import relativedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ============================================================
# 기본 설정
# ============================================================
KST = timezone(timedelta(hours=9))

def get_now():
    return datetime.now(KST)

st.set_page_config(page_title="대웅 법무 업무 현황", layout="wide")

# 대웅 테마
DW_ORANGE = '#FF6B00'
DW_DARK = '#333333'

DONE_STATUSES = [
    '법무 승인 중', '법무 확인 완료',
    '결재 생략', '서명 생략', '서명 진행 중',
    '종료(체결)', '종료(미체결)'
]
ACTIVE_STATUS = '법무 검토 중'

# 영구 대시보드 URL (이메일 본문에 들어감)
DASHBOARD_URL = "https://daewoong-contract-dashboard.streamlit.app"

# ============================================================
# 데이터 로드 (Google Drive API)
# ============================================================
@st.cache_data(ttl=300)
def load_data():
    credentials = service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    service = build("drive", "v3", credentials=credentials)
    folder_id = st.secrets["drive"]["folder_id"]

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
        st.error("Google Drive 폴더에 파일이 없습니다.")
        st.stop()

    latest = files[0]

    request = service.files().get_media(fileId=latest["id"])
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)

    if latest["name"].endswith(".xlsx"):
        df = pd.read_excel(fh)
    else:
        try:
            df = pd.read_csv(fh, encoding="utf-8-sig")
        except UnicodeDecodeError:
            fh.seek(0)
            df = pd.read_csv(fh, encoding="cp949")

    # 데이터 클리닝
    if '계약 상태' in df.columns:
        df['계약 상태'] = df['계약 상태'].astype(str).str.replace('법무 확인완료', '법무 확인 완료')

    df['배정된 검토자'] = df['배정된 검토자'].fillna('⚠️ 미배정')

    def is_valid(name):
        n = str(name)
        return bool(re.search('[가-힣]', n)) and '삭제됨' not in n

    df = df[df['배정된 검토자'].apply(lambda x: x == '⚠️ 미배정' or is_valid(x))]

    for col in ['의뢰일', '최종 검토 회신일']:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors='coerce')

    return df, latest["name"], latest["modifiedTime"][:10]

# ============================================================
# 데이터 로드 실행
# ============================================================
df, file_name, file_date = load_data()
staff_list = sorted([s for s in df['배정된 검토자'].unique() if s != '⚠️ 미배정'])
now_kst = get_now()
now_naive = now_kst.replace(tzinfo=None)

# ============================================================
# HEADER (좌: 대웅 로고 / 중앙: 제목 / 우: 법무팀 이미지)
# ============================================================
col_logo_left, col_title, col_logo_right = st.columns([1, 5, 1])

with col_logo_left:
    if os.path.exists('dw_logo.png'):
        st.image('dw_logo.png', width=140)

with col_title:
    st.title("⚖️ 대웅 법무 업무 현황")
    st.markdown(
        f"<p style='color:{DW_ORANGE}; font-weight:bold; margin-top:-15px;'>"
        f"Daewoong Legal Task Status | "
        f"<span style='font-size:1.2rem;'>{now_kst.strftime('%Y-%m-%d %H:%M')} (KST)</span>"
        f"</p>",
        unsafe_allow_html=True
    )
    st.caption(f"데이터 파일: {file_name} ({file_date})")

with col_logo_right:
    if os.path.exists('legal_team.png'):
        st.image('legal_team.png', width=140)

# 새로고침 버튼
if st.button("🔄 데이터 새로고침"):
    load_data.clear()
    st.rerun()

st.markdown("---")

# ============================================================
# SECTION 1: KPI
# ============================================================
done_df = df[df['계약 상태'].isin(DONE_STATUSES)].copy()
active_df = df[df['계약 상태'] == ACTIVE_STATUS].copy()

def get_count(m_dt):
    m_str = m_dt.strftime('%Y-%m')
    if done_df.empty:
        return 0
    return len(done_df[done_df['최종 검토 회신일'].dt.strftime('%Y-%m') == m_str])

c_m0 = get_count(now_naive)
c_m1 = get_count(now_naive - relativedelta(months=1))
c_m2 = get_count(now_naive - relativedelta(months=2))

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("🔥 현재 검토 중", f"{len(active_df)}건")
k2.metric("✅ 이번달 완료", f"{c_m0}건")
k3.metric("📅 지난달 완료", f"{c_m1}건")
k4.metric("📅 지지난달 완료", f"{c_m2}건")
k5.metric("👥 검토 인력", f"{len(staff_list)}명")

# ============================================================
# SECTION 2: 계약검토 배정 현황
# ============================================================
st.markdown("###")
st.subheader("👤 계약검토 배정 현황")

load_summary = []
for s in staff_list:
    tasks = active_df[active_df['배정된 검토자'] == s]
    count = len(tasks)
    titles = "<br>• ".join(tasks['계약명'].head(15).tolist()) if count > 0 else "업무 없음"
    load_summary.append({'검토자': s, '건수': count, '상세목록': titles})

load_df = pd.DataFrame(load_summary).sort_values(by='건수', ascending=False)

fig_load = px.bar(
    load_df,
    x='검토자', y='건수',
    color='건수',
    color_continuous_scale=[DW_DARK, DW_ORANGE],
    text_auto=True,
    custom_data=['상세목록']
)
fig_load.update_xaxes(categoryorder='total descending')
fig_load.update_traces(
    hovertemplate="<b>%{x}</b><br>배정 건수: %{y}건<br><b>[목록]</b><br>%{customdata[0]}"
)
st.plotly_chart(fig_load, use_container_width=True)
st.caption("💡 막대에 마우스를 올리면 상세 계약 목록을 확인할 수 있습니다.")

# ============================================================
# SECTION 3: 월별 검토 완료 건수
# ============================================================
st.markdown("---")
st.subheader("📊 월별 검토 완료 건수")

cp1, _ = st.columns([2, 8])
p_start = cp1.date_input("조회 시작일", (now_naive - relativedelta(months=3)).date())
p_end = cp1.date_input("조회 종료일", now_naive.date())

p_df = df[
    (df['계약 상태'].isin(DONE_STATUSES))
    & (df['최종 검토 회신일'].dt.date >= p_start)
    & (df['최종 검토 회신일'].dt.date <= p_end)
].copy()

fig_perf = None
if not p_df.empty:
    p_df = p_df.sort_values('최종 검토 회신일')
    p_df['완료월'] = p_df['최종 검토 회신일'].dt.strftime('%y.%m')
    perf_grouped = p_df.groupby(['배정된 검토자', '완료월'], sort=False).size().reset_index(name='완료건수')

    h_texts = []
    for _, row in perf_grouped.iterrows():
        titles = p_df[
            (p_df['배정된 검토자'] == row['배정된 검토자'])
            & (p_df['완료월'] == row['완료월'])
        ]['계약명'].head(10).tolist()
        h_texts.append("<br>• ".join(titles))
    perf_grouped['상세목록'] = h_texts

    month_order = sorted(perf_grouped['완료월'].unique())

    fig_perf = px.bar(
        perf_grouped,
        x='배정된 검토자', y='완료건수',
        color='완료월',
        barmode='group',
        color_discrete_sequence=px.colors.qualitative.T10,
        custom_data=['상세목록'],
        category_orders={"완료월": month_order}
    )
    fig_perf.update_traces(
        hovertemplate="<b>%{x}</b> (%{fullData.name})<br>완료: %{y}건<br><b>[목록]</b><br>%{customdata[0]}"
    )
    st.plotly_chart(fig_perf, use_container_width=True)
    st.caption("💡 막대에 마우스를 올리면 상세 계약 목록을 확인할 수 있습니다.")
else:
    st.info("선택한 기간에 완료된 검토가 없습니다.")

# ============================================================
# SECTION 4: 부서 및 유형 분석
# ============================================================
st.markdown("---")
c_pie, c_bar = st.columns(2)

fig_pie = None
with c_pie:
    st.subheader("🏢 의뢰 부서별 비중")
    if '의뢰부서' in df.columns:
        dept_counts = df['의뢰부서'].value_counts().head(10).reset_index()
        dept_counts.columns = ['의뢰부서', 'count']
        fig_pie = px.pie(
            dept_counts,
            names='의뢰부서', values='count',
            hole=0.4,
            color_discrete_sequence=px.colors.qualitative.Pastel
        )
        st.plotly_chart(fig_pie, use_container_width=True)
    else:
        st.info("'의뢰부서' 컬럼이 데이터에 없습니다.")

with c_bar:
    st.subheader("📂 업무 유형별 분포")
    if '계약 분류' in df.columns:
        type_counts = df['계약 분류'].value_counts().head(10).reset_index()
        type_counts.columns = ['계약 분류', 'count']
        fig_type = px.bar(
            type_counts,
            x='count', y='계약 분류',
            orientation='h',
            color='계약 분류',
            color_discrete_sequence=px.colors.qualitative.Safe
        )
        fig_type.update_layout(showlegend=False)
        st.plotly_chart(fig_type, use_container_width=True)
    else:
        st.info("'계약 분류' 컬럼이 데이터에 없습니다.")

# ============================================================
# SECTION 5: 장기 미결 업무 관리
# ============================================================
st.markdown("---")
st.subheader("🛡️ 장기 미결 업무 관리")

stale_tasks = active_df[active_df['의뢰일'] < (now_naive - timedelta(days=14))].head(20)
if not stale_tasks.empty:
    st.warning(f"현재 {len(stale_tasks)}건의 장기 지연 업무가 확인되었습니다.")
    cols_to_show = [c for c in ['의뢰일', '의뢰부서', '계약명', '배정된 검토자'] if c in stale_tasks.columns]
    st.dataframe(stale_tasks[cols_to_show], use_container_width=True)
else:
    st.success("✅ 현재 지연된 업무가 없습니다.")

# ============================================================
# 이메일 발송 섹션
# ============================================================
st.markdown("---")
st.subheader("📩 업무 현황 리포트 발송")

# Secrets에 이메일 설정이 있는지 확인
email_configured = "email" in st.secrets

if not email_configured:
    st.info(
        "ℹ️ 이메일 발송 기능을 사용하려면 Streamlit Secrets에 이메일 설정을 추가해야 합니다. "
        "설정 방법은 관리자에게 문의하세요."
    )
else:
    col_f1, col_f2 = st.columns([6, 2])
    receiver_input = col_f1.text_input("메일 주소 입력", value="@daewoong.co.kr")

    if col_f2.button("🚀 리포트 발송", use_container_width=True):
        with st.spinner("리포트 생성 중..."):
            try:
                S_MAIL = st.secrets["email"]["sender"]
                S_PW = st.secrets["email"]["password"]

                img_load = pio.to_image(fig_load, format='png', width=1000, height=500, engine='kaleido')
                img_perf = pio.to_image(fig_perf, format='png', width=1000, height=500, engine='kaleido') if fig_perf else None
                img_pie = pio.to_image(fig_pie, format='png', width=800, height=500, engine='kaleido') if fig_pie else None

                msg = MIMEMultipart('related')
                msg['Subject'] = f"[대웅법무] 업무 현황 리포트 ({now_kst.strftime('%y.%m.%d')})"
                msg['From'] = S_MAIL
                msg['To'] = receiver_input

                url_section = (
                    f'<div style="margin-top:40px; padding:20px; background:#f0f0f0; border-radius:10px; text-align:center;">'
                    f'<h4 style="margin:0;">🔗 업무 현황 대시보드 바로가기</h4>'
                    f'<p style="font-size:13px; color:#666; margin:10px 0;">상세 데이터 필터링이 가능한 웹 리포트입니다.</p>'
                    f'<a href="{DASHBOARD_URL}" style="display:inline-block; padding:12px 25px; background:{DW_ORANGE}; color:white; text-decoration:none; border-radius:5px; font-weight:bold;">대시보드 접속하기</a>'
                    f'</div>'
                )

                html_mail = (
                    f'<html><body style="font-family:Malgun Gothic; padding:20px; background:#f9f9f9;">'
                    f'<div style="max-width:850px; margin:auto; background:white; padding:30px; border-radius:15px; border:1px solid #ddd;">'
                    f'<div style="background:{DW_ORANGE}; padding:20px; text-align:center; color:white; border-radius:10px 10px 0 0;">'
                    f'<h1 style="margin:0;">대웅 법무 업무 현황 리포트</h1></div>'
                    f'<div style="padding:20px;">'
                    f'<p style="text-align:right; font-size:18px; color:#333; font-weight:bold;">기준 시각: {now_kst.strftime("%Y-%m-%d %H:%M")} (KST)</p>'
                    f'<h3 style="color:{DW_ORANGE}; border-left:6px solid {DW_ORANGE}; padding-left:12px;">🚀 핵심 지표 요약</h3>'
                    f'<table style="width:100%; text-align:center; border-collapse:collapse; margin-bottom:25px; background:#fffbf7;">'
                    f'<tr><td style="padding:15px; border:1px solid #eee;"><b>현재 검토 중</b><br><span style="font-size:22px; color:red; font-weight:bold;">{len(active_df)}건</span></td>'
                    f'<td style="padding:15px; border:1px solid #eee;"><b>이번달 완료</b><br><span style="font-size:22px; color:blue; font-weight:bold;">{c_m0}건</span></td>'
                    f'<td style="padding:15px; border:1px solid #eee;"><b>지난달 완료</b><br><span style="font-size:22px; color:#333; font-weight:bold;">{c_m1}건</span></td></tr></table>'
                    f'<h3 style="color:{DW_ORANGE};">👤 계약검토 배정 현황</h3>'
                    f'<img src="cid:load" style="width:100%; border:1px solid #eee; margin-bottom:25px;">'
                )

                if img_perf:
                    html_mail += (
                        f'<h3 style="color:{DW_ORANGE};">📊 월별 검토 완료 건수</h3>'
                        f'<img src="cid:perf" style="width:100%; border:1px solid #eee; margin-bottom:25px;">'
                    )
                if img_pie:
                    html_mail += (
                        f'<h3 style="color:{DW_ORANGE};">🏢 의뢰 부서별 비중</h3>'
                        f'<img src="cid:pie" style="width:100%; border:1px solid #eee;">'
                    )

                html_mail += f'{url_section}</div></div></body></html>'

                msg.attach(MIMEText(html_mail, 'html'))

                attachments = [(img_load, 'load')]
                if img_perf:
                    attachments.append((img_perf, 'perf'))
                if img_pie:
                    attachments.append((img_pie, 'pie'))

                for data, cid in attachments:
                    img = MIMEImage(data)
                    img.add_header('Content-ID', f'<{cid}>')
                    msg.attach(img)

                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
                    s.login(S_MAIL, S_PW)
                    s.sendmail(S_MAIL, receiver_input, msg.as_string())
                st.success(f"✅ {receiver_input}로 리포트 발송 완료!")
            except Exception as e:
                st.error(f"실패: {e}")
