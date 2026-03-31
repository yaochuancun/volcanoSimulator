"""按算法绘制 Makespan（尾延迟）柱状图。"""

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def auto_label(rects):
    """在柱顶标注整型高度（略上移避免与柱体重叠）。"""
    for rect in rects:
        height = rect.get_height()
        plt.text(rect.get_x() + rect.get_width() / 2. - 0.35, 1.01 * height, str(int(height)), fontsize=5)


def draw_makespan(data_frame: pd.DataFrame,
                  algorithm_names,
                  title: str = None,
                  x_label: str = None,
                  y_label: str = 'Tail latencies(s)'):
    """从 summary 中取各算法 makespan 画柱状图；DRL 可用不同颜色高亮。"""
    means = []
    colorid = []
    for an in algorithm_names:
        if an == "DRL":
            colorid.append("blue")
        else:
            colorid.append("black")
        df = data_frame[data_frame['name'] == an]
        mean = df['makespan'].mean()
        means.append(mean)

    x = algorithm_names
    y = means

    plt.xticks(np.arange(len(x)), x, rotation=-270)
    a = plt.bar(np.arange(len(x)), y, color=colorid)
    auto_label(a)

    plt.ylim(0, int(max(means)+200))
    plt.title(title, fontsize=12)
    plt.xlabel(x_label, fontsize=12)
    plt.ylabel(y_label, fontsize=12)

    plt.grid(True)
