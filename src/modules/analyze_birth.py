import numpy as np
import pandas as pd


def simulate_future_births():
    # 1. 录入图片 1000091551.jpg 中的历史真实新出生人口数据 (单位：万人)
    # 预测 2026-2070 年需要用到 1997-2041 年的人口基数作为“母亲辈”
    births = {
        1996: 2078,
        1997: 2038,
        1998: 1991,
        1999: 1909,
        2000: 1778,
        2001: 1702,
        2002: 1647,
        2003: 1599,
        2004: 1593,
        2005: 1617,
        2006: 1584,
        2007: 1594,
        2008: 1608,
        2009: 1615,
        2010: 1596,
        2011: 1604,
        2012: 1635,
        2013: 1640,
        2014: 1687,
        2015: 1655,
        2016: 1786,
        2017: 1723,
        2018: 1523,
        2019: 1465,
        2020: 1203,
        2021: 1062,
        2022: 956,
        2023: 902,
        2024: 954,
        2025: 792,
    }

    # 2. 定义不同情景下的代际收缩因子 (Generation Contraction Factor)
    # 算法逻辑：下一代人口 = 29年前基数 * 收缩因子
    # 收缩因子粗略等于：(TFR / 2.1) * 存活与性别比校正
    scenarios = {
        "低线情景 (TFR ~ 0.7)": 0.33,
        "中线情景 (TFR ~ 0.95)": 0.43,
        "激进补贴 (TFR ~ 1.3)": 0.60,
    }

    # 初始化存储预测结果的字典
    predictions = {scen: births.copy() for scen in scenarios}

    # 3. 核心循环：逐年推演 2026 到 2070 年
    start_year = 2026
    end_year = 2100
    generation_gap = 29  # 假设平均生育年龄为 29 岁

    for year in range(start_year, end_year + 1):
        mother_year = year - generation_gap

        for name, factor in scenarios.items():
            # 基础结构性出生人口
            structural_births = predictions[name][mother_year] * factor

            # 平滑过渡算法：避免 2026 年因模型切换出现数据断层
            # 2026-2035 年间，由 2025 年的真实值 (792万) 逐步向模型结构值过渡
            if year <= 2035:
                weight = (year - 2025) / 10
                current_births = 792.0 * (1 - weight) + structural_births * weight
            else:
                current_births = structural_births

            predictions[name][year] = round(current_births, 1)

    # 4. 格式化打印输出表格
    print(
        f"{'年份':<6} | {'低线情景(万)':<12} | {'中线情景(万)':<12} | {'激进补贴(万)':<12}"
    )
    print("-" * 52)

    # 打印每一年的数据（如果你只想看5年节点，可以改为 if y % 5 == 0）
    for y in range(start_year, end_year + 1, 5):
        print(f"{y:<8} | {predictions['低线情景 (TFR ~ 0.7)'][y]:<14} | \
                {predictions['中线情景 (TFR ~ 0.95)'][y]:<14} | \
                    {predictions['激进补贴 (TFR ~ 1.3)'][y]:<14}")

    # 将数据转化成 dataframe 在存到excel中
    df = pd.DataFrame(
        {
            "年份": list(predictions["低线情景 (TFR ~ 0.7)"].keys()),
            "低线情景(万)": list(predictions["低线情景 (TFR ~ 0.7)"].values()),
            "中线情景(万)": list(predictions["中线情景 (TFR ~ 0.95)"].values()),
            "激进补贴(万)": list(predictions["激进补贴 (TFR ~ 1.3)"].values()),
        }
    )
    df.to_excel("data/birth_predictions.xlsx", index=False)


def analyze_demographics_with_thresholds(
    old_threshold=60,
    youth_threshold=22,
    max_age=79,
    birth_scenario="中线情景 (TFR ~ 0.95)",
):
    """
    参数:
    old_threshold:   进入老龄/弱体力状态的年龄 (默认 60 岁)
    youth_threshold: 真正进入职场创造产出的年龄 (默认 22 岁)
    max_age:         依据国家卫健委最新数据的平均预期寿命 (设定为 79 岁)
    """
    # 1. 1945-1990 历史婴儿潮与调整期数据锚点
    anchors = {
        1945: 1200,
        1950: 1800,
        1955: 2000,
        1960: 1400,
        1962: 2450,
        1965: 2700,
        1970: 2788,
        1975: 2138,
        1980: 1797,
        1985: 2227,
        1990: 2408,
    }
    # 1991-2025 真实历史精确新生儿数据 (单位：万人)
    exact_hist = [
        2279,
        2137,
        2144,
        2121,
        2074,
        2078,
        2038,
        1991,
        1909,
        1778,
        1702,
        1647,
        1599,
        1593,
        1617,
        1584,
        1594,
        1608,
        1615,
        1596,
        1604,
        1635,
        1640,
        1687,
        1655,
        1786,
        1723,
        1523,
        1465,
        1203,
        1062,
        956,
        902,
        954,
        792,
    ]

    # 插值补全 1945-1990 年的数据
    hist_years = np.arange(1945, 1991)
    hist_births = np.interp(hist_years, list(anchors.keys()), list(anchors.values()))

    birth_series = {int(y): b for y, b in zip(hist_years, hist_births)}
    for y, b in zip(range(1991, 2026), exact_hist):
        birth_series[y] = b

    # 2. 定义不同情景下的代际收缩因子 (Generation Contraction Factor)
    # 算法逻辑：下一代人口 = 29年前基数 * 收缩因子
    # 收缩因子粗略等于：(TFR / 2.1) * 存活与性别比校正
    scenarios = {
        "低线情景 (TFR ~ 0.7)": 0.33,
        "中线情景 (TFR ~ 0.95)": 0.43,
        "激进补贴 (TFR ~ 1.3)": 0.60,
    }

    pred_year = 2100

    for y in range(2026, pred_year + 1):
        structural = birth_series[y - 29] * scenarios[birth_scenario]
        if y <= 2035:
            weight = (y - 2025) / 10
            birth_series[y] = 792.0 * (1 - weight) + structural * weight
        else:
            birth_series[y] = structural

    # 3. 逐年计算特定年龄群体的占比
    target_years = list(range(2025, pred_year + 1, 5))
    results = []

    for y in target_years:
        youth_pop = 0  # 年轻一代 (< youth_threshold)
        worker_pop = 0  # 核心劳动力 (youth_threshold 到 old_threshold - 1)
        elderly_pop = 0  # 老龄人口 (old_threshold 到 max_age)

        for age in range(0, max_age + 1):
            born_year = y - age
            cohort_size = birth_series.get(born_year, 1200)

            # 高龄阶段自然消退折减 (接近预期寿命时加速消退)
            if age > (max_age - 5):
                cohort_size *= 1 - (age - (max_age - 5)) * 0.18
                if cohort_size < 0:
                    cohort_size = 0

            if age < youth_threshold:
                youth_pop += cohort_size
            elif youth_threshold <= age < old_threshold:
                worker_pop += cohort_size
            else:
                elderly_pop += cohort_size

        total_pop = youth_pop + worker_pop + elderly_pop

        aging_rate = (elderly_pop / total_pop) * 100
        youth_ratio = (youth_pop / total_pop) * 100
        core_worker_ratio = (worker_pop / total_pop) * 100

        results.append(
            [
                y,
                f"{aging_rate:.1f}%",
                f"{youth_ratio:.1f}%",
                f"{core_worker_ratio:.1f}%",
                f"{total_pop/10000:.2f}亿",
            ]
        )

    df = pd.DataFrame(
        results,
        columns=[
            "年份",
            f"真实老龄化率(>={old_threshold}岁)",
            f"年轻一代比例(<{youth_threshold}岁)",
            "核心顶梁柱劳动力比例",
            "总人口模拟",
        ],
    )

    print(f"=== 基于国家官方预期寿命 ({max_age}岁) 的结构推演 ===")
    print(f"情景: {birth_scenario}")
    print(df.to_string(index=False))

    # 将结果保存到 Excel 文件
    df.to_excel(
        f"data/demographics_{birth_scenario.replace(' ', '_')}.xlsx", index=False
    )


if __name__ == "__main__":
    simulate_future_births()

    # 运行模型
    analyze_demographics_with_thresholds(
        old_threshold=65,
        youth_threshold=22,
        max_age=79,
        birth_scenario="激进补贴 (TFR ~ 1.3)",
    )
    analyze_demographics_with_thresholds(
        old_threshold=65,
        youth_threshold=22,
        max_age=79,
        birth_scenario="中线情景 (TFR ~ 0.95)",
    )
    analyze_demographics_with_thresholds(
        old_threshold=65,
        youth_threshold=22,
        max_age=79,
        birth_scenario="低线情景 (TFR ~ 0.7)",
    )
