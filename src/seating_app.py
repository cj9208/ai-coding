import streamlit as st
import pandas as pd
import random
import io

# --- 核心算法：前排优先 + 社交隔离 ---
def generate_seating_chart(members, talkative_members, rows, cols):
    num_people = len(members)
    num_seats = rows * cols
    
    if num_people > num_seats:
        return None, f"错误：座位数({num_seats})少于总人数({num_people})。"
    
    # 确定必须坐人的位置 (从前向后填满)
    active_seats = []
    for r in range(rows):
        for c in range(cols):
            if len(active_seats) < num_people:
                active_seats.append((r, c))
    
    for attempt in range(3000):
        chart = [[None for _ in range(cols)] for _ in range(rows)]
        # 标记空白区
        all_possible_seats = [(r, c) for r in range(rows) for c in range(cols)]
        for seat in all_possible_seats:
            if seat not in active_seats:
                chart[seat[0]][seat[1]] = "" 

        current_talkative = list(talkative_members)
        random.shuffle(current_talkative)
        
        available_active_seats = list(active_seats)
        random.shuffle(available_active_seats)
        
        success = True
        for talker in current_talkative:
            found_seat = False
            for i, (r, c) in enumerate(available_active_seats):
                # 严格隔离校验
                is_safe = True
                for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < rows and 0 <= nc < cols:
                        if chart[nr][nc] in talkative_members:
                            is_safe = False
                            break
                
                if is_safe:
                    chart[r][c] = talker
                    available_active_seats.pop(i)
                    found_seat = True
                    break
            
            if not found_seat:
                success = False
                break
        
        if success:
            others = [m for m in members if m not in talkative_members]
            random.shuffle(others)
            for r, c in available_active_seats:
                if others:
                    chart[r][c] = others.pop()
            return chart, None

    return None, "无法找到符合要求的排座方案，请增加座位数或减少活跃成员。"

# --- Excel 导出辅助函数 ---
def to_excel_with_index(df):
    output = io.BytesIO()
    # 增加行序号：第一排，第二排...
    df_export = df.copy()
    df_export.index = [f"第 {i+1} 排" for i in range(len(df_export))]
    df_export.columns = [f"第 {i+1} 列" for i in range(len(df_export.columns))]
    
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df_export.to_excel(writer, sheet_name='排座表')
    return output.getvalue()

# --- Streamlit 界面 ---
st.set_page_config(page_title="专业智能排座系统", layout="wide")

if 'talkers' not in st.session_state:
    st.session_state.talkers = set()

st.title("🪑 专业智能排座系统")
st.markdown("💡 **规则：** 爱说话者不相邻 | 前排优先坐满 | 导出 Excel 带序号")

# 侧边栏：文件处理
uploaded_file = st.sidebar.file_uploader("1. 上传名单 Excel", type=["xlsx"])
names = []

if uploaded_file:
    xl = pd.ExcelFile(uploaded_file)
    sheet = st.sidebar.selectbox("2. 选择 Sheet", xl.sheet_names)
    df = pd.read_excel(uploaded_file, sheet_name=sheet)
    if not df.empty:
        col = st.sidebar.selectbox("3. 选择姓名列", df.columns.tolist())
        names = df[col].dropna().astype(str).tolist()

# 主界面：名单交互
if names:
    st.subheader(f"名单确认 ({len(names)}人)：点击名字标记“活跃分子”")
    cols_per_row = 10
    for i in range(0, len(names), cols_per_row):
        cols = st.columns(cols_per_row)
        for j, name in enumerate(names[i:i+cols_per_row]):
            is_selected = name in st.session_state.talkers
            if cols[j].button(name, key=f"btn_{name}", use_container_width=True, 
                             type="primary" if is_selected else "secondary"):
                if name in st.session_state.talkers:
                    st.session_state.talkers.remove(name)
                else:
                    st.session_state.talkers.add(name)
                st.rerun()

    st.divider()
    
    # 布局设置
    c1, c2 = st.columns(2)
    rows_num = c1.number_input("教室总行数", min_value=1, value=6)
    cols_num = c2.number_input("教室总列数", min_value=1, value=6)
    
    if st.button("🚀 生成并导出排座表", type="primary", use_container_width=True):
        result, error = generate_seating_chart(names, list(st.session_state.talkers), rows_num, cols_num)
        
        if error:
            st.error(error)
        else:
            st.success("排座成功！预览如下：")
            res_df = pd.DataFrame(result).fillna("")
            
            # 屏幕展示样式
            def style_cells(val):
                if val in st.session_state.talkers:
                    return 'background-color: #ff4b4b; color: white; font-weight: bold'
                if val == "":
                    return 'background-color: #f0f2f6'
                return ''
            
            # 展示预览，带排号列
            display_df = res_df.copy()
            display_df.index = [f"第 {i+1} 排" for i in range(len(display_df))]
            st.table(display_df.style.applymap(style_cells))
            
            # 导出 Excel
            excel_data = to_excel_with_index(res_df)
            st.download_button(
                label="📥 点击下载 Excel 排座表 (带行号)",
                data=excel_data,
                file_name="智能排座表_导出.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
else:
    st.info("👋 请先在左侧侧边栏上传 Excel 名单文件。")