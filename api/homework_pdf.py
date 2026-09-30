# -*- coding: utf-8 -*-
"""
作业题面与答案 PDF 导出模块

将作业答题页解析得到的题目信息(题干/选项/脚本填写的答案)排版为 PDF,
便于刷课完成后线下复习或核对。通过 [homework] 节的 pdf=1/0 开关控制。

实现基于 reportlab, 使用其内置的 STSong-Light(UniGB-UCS2-H CMap) 渲染中文,
无需在系统中安装额外中文字体文件。
"""
import os
import re
import time
from datetime import datetime

from loguru import logger


# 题型码 -> 中文名
_TYPE_LABELS = {
    "single": "单选题",
    "multiple": "多选题",
    "completion": "填空题",
    "judgement": "判断题",
    "shortanswer": "简答题",
    "unknown": "未知题型",
}

# 判断题答案映射
_JUDGEMENT_LABELS = {
    "true": "正确",
    "false": "错误",
}


def _sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip()
    return name or "homework"


def _option_lookup(q: dict) -> dict:
    """构建选项字母 -> 选项文本的映射, 用于把字母答案还原成文本"""
    lookup = {}
    for line in (q.get("options") or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        letter = parts[0].strip()
        if letter:
            lookup[letter] = parts[1].strip() if len(parts) > 1 else ""
    return lookup


def _render_answer(q: dict) -> str:
    """把脚本填写的答案渲染为可读文本(字母答案还原为对应选项文本)"""
    raw = (q.get("answerField") or {}).get(f"answer{q['id']}", "") or ""
    if not raw:
        return "(空)"

    q_type = q.get("type")
    if q_type == "judgement":
        return _JUDGEMENT_LABELS.get(raw.lower(), raw)

    if q_type in ("single", "multiple"):
        lookup = _option_lookup(q)
        rendered = []
        for ch in raw:
            ch = ch.upper()
            text = lookup.get(ch)
            rendered.append(f"{ch}({text})" if text else ch)
        if rendered:
            return "、".join(rendered)
    return raw


def _build_story(course: dict, work: dict, form: dict) -> list:
    """构建 PDF 内容流(flowables), 便于单测"""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer

    body_style = ParagraphStyle(
        "body",
        fontName="STSong-Light",
        fontSize=11,
        leading=16,
        alignment=TA_LEFT,
        wordWrap="CJK",
        spaceAfter=2 * mm,
    )
    option_style = ParagraphStyle(
        "option",
        parent=body_style,
        leftIndent=8 * mm,
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#333333"),
    )
    answer_style = ParagraphStyle(
        "answer",
        parent=body_style,
        leftIndent=4 * mm,
        textColor=colors.HexColor("#006699"),
    )

    story = []
    course_title = _sanitize_filename(course.get("title") or course.get("courseId") or "课程")
    story.append(Paragraph(f"作业题面与答案 - {course_title}", ParagraphStyle(
        "title",
        fontName="STSong-Light",
        fontSize=16,
        leading=22,
        alignment=TA_LEFT,
        spaceAfter=4 * mm,
    )))
    questions = form.get("questions") or []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"作业 workId={work.get('workId', '')}<br/>生成时间: {now}&nbsp;&nbsp;共 {len(questions)} 题"
    story.append(Paragraph(header, ParagraphStyle(
        "header",
        fontName="STSong-Light",
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#888888"),
        spaceAfter=4 * mm,
    )))

    answers_section = []
    for idx, q in enumerate(questions, start=1):
        type_label = _TYPE_LABELS.get(q.get("type"), "未知题型")
        title = q.get("title") or ""
        # 超星题干可能含 <img ...> 占位, 简单清理避免破坏 PDF 布局
        title = re.sub(r"<img[^>]*>", "[图片]", title)
        title = re.sub(r"<[^>]+>", "", title)

        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(
            f"{idx}. [{type_label}] {title}",
            body_style,
        ))

        for line in (q.get("options") or "").split("\n"):
            line = line.strip()
            if not line:
                continue
            story.append(Paragraph(line, option_style))

        answer = _render_answer(q)
        answers_section.append(Paragraph(f"{idx}. {answer}", answer_style))

    # 所有答案统一排版在 PDF 最后
    if answers_section:
        answers_section.insert(0, Spacer(1, 4 * mm))
        answers_section.insert(1, Paragraph("参考答案", ParagraphStyle(
            "answer_title",
            fontName="STSong-Light",
            fontSize=14,
            leading=20,
            alignment=TA_LEFT,
            spaceAfter=3 * mm,
        )))
        story.extend(answers_section)

    return story


def generate_homework_pdf(course: dict, work: dict, form: dict, output_dir: str = "homework_pdf") -> str:
    """
    生成作业题面与答案 PDF 文件。

    Args:
        course: 课程信息(courseId/cpi/title 等)
        work: 作业信息(workId 等)
        form: 作业答题页解析结果(questions 中应已填写 answerField)
        output_dir: PDF 输出目录

    Returns:
        生成的 PDF 文件绝对路径

    Raises:
        ImportError: reportlab 未安装时抛出
        HomeworkError: 生成失败时抛出
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import SimpleDocTemplate
    except ImportError as e:
        logger.error(f"导出 PDF 需要 reportlab, 请先安装: pip install -r requirements.txt ({e})")
        raise

    os.makedirs(output_dir, exist_ok=True)

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    course_title = _sanitize_filename(course.get("title") or course.get("courseId") or "课程")
    work_id = _sanitize_filename(str(work.get("workId", int(time.time()))))
    file_name = f"{course_title}_{work_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
    file_path = os.path.abspath(os.path.join(output_dir, file_name))

    doc = SimpleDocTemplate(
        file_path,
        pagesize=A4,
        title=f"作业题面与答案 - {course_title}",
        author="SuperStar 刷课",
    )
    doc.build(_build_story(course, work, form))

    logger.info(f"作业 PDF 已生成 -> {file_path}")
    return file_path