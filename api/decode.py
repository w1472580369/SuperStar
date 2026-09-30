# -*- coding: utf-8 -*-
"""
超星学习通数据解析模块

该模块负责解析超星学习通平台的课程、章节、任务点等各种数据，
并转换为程序内部使用的结构化数据格式。
"""
import json
import re
from typing import List, Dict, Tuple, Any, Optional

from bs4 import BeautifulSoup, NavigableString

from api.font_decoder import FontDecoder
from api.logger import logger


def decode_course_list(html_text: str) -> List[Dict[str, str]]:
    """
    解析课程列表页面，提取课程信息
    
    Args:
        html_text: 课程列表页面的HTML内容
        
    Returns:
        课程信息列表，每个课程包含id、title、teacher等信息
    """
    logger.trace("开始解码课程列表...")
    soup = BeautifulSoup(html_text, "lxml")
    raw_courses = soup.select("div.course")
    course_list = []
    
    for course in raw_courses:
        # 跳过未开放课程
        if course.select_one("a.not-open-tip") or course.select_one("div.not-open-tip"):
            continue
        
        course_detail = {
            "id": course.attrs["id"],
            "info": course.attrs["info"],
            "roleid": course.attrs["roleid"],
            "clazzId": course.select_one("input.clazzId").attrs["value"],
            "courseId": course.select_one("input.courseId").attrs["value"],
            "cpi": re.findall(r"cpi=(.*?)&", course.select_one("a").attrs["href"])[0],
            "title": course.select_one("span.course-name").attrs["title"],
            "desc": course.select_one("p.margint10").attrs["title"] if course.select_one("p.margint10") else "",
            "teacher": course.select_one("p.color3").attrs["title"]
        }
        course_list.append(course_detail)
    
    return course_list


def decode_course_folder(html_text: str) -> List[Dict[str, str]]:
    """
    解析二级课程列表页面，提取文件夹信息
    
    Args:
        html_text: 二级课程列表页面的HTML内容
        
    Returns:
        课程文件夹信息列表
    """
    logger.trace("开始解码二级课程列表...")
    soup = BeautifulSoup(html_text, "lxml")
    raw_courses = soup.select("ul.file-list>li")
    course_folder_list = []
    
    for course in raw_courses:
        if not course.attrs.get("fileid"):
            continue
            
        course_folder_detail = {
            "id": course.attrs["fileid"],
            "rename": course.select_one("input.rename-input").attrs["value"]
        }
        course_folder_list.append(course_folder_detail)
    
    return course_folder_list


def decode_course_point(html_text: str) -> Dict[str, Any]:
    """
    解析章节列表页面，提取章节点信息
    
    Args:
        html_text: 章节列表页面的HTML内容
        
    Returns:
        章节信息字典，包含是否锁定状态和章节点列表
    """
    logger.trace("开始解码章节列表...")
    soup = BeautifulSoup(html_text, "lxml")
    course_point = {
        "hasLocked": False,  # 用于判断该课程任务是否是需要解锁
        "points": [],
    }

    for chapter_unit in soup.find_all("div", class_="chapter_unit"):
        points = _extract_points_from_chapter(chapter_unit)
        # 检查是否有锁定内容
        for point in points:
            if point.get("need_unlock", False):
                course_point["hasLocked"] = True
                
        course_point["points"].extend(points)
    
    return course_point


def _element_text(element) -> str:
    """安全提取元素文本, 元素不存在时返回空字符串。"""
    if element is None:
        return ""
    return element.get_text(strip=True)


def _element_classes(element) -> str:
    """拼接元素的所有 class, 便于按关键字匹配图标状态。"""
    if element is None:
        return ""
    classes = element.get("class") or []
    return " ".join(str(cls) for cls in classes)


def _detect_completed_icon(point) -> bool:
    """
    通过任务点下的状态图标判断是否已完成。

    超星新版网页会用 icon_Completed / icon_NotCompleted 这类元素表示章节状态,
    这些图标可能只带样式而不含文字, 因此不能只看 bntHoverTips 的文本。
    """
    for icon in point.find_all(["em", "i", "span"]):
        classes = _element_classes(icon)
        if any(key in classes for key in ("NotCompleted", "Uncompleted", "Lock")):
            return False
        if "Completed" in classes:
            return True
    return False


def _is_point_finished(point) -> bool:
    """
    判断章节任务点是否已全部完成。

    优先依据提示文案: 只要出现“未完成”就一定视为未完成, 避免新版页面同时
    渲染“已完成/未完成”两段文案时把未完成的章节误判为已看完; 其次回退到图标判断。
    """
    tips_text = _element_text(point.select_one("span.bntHoverTips"))
    if "未完成" in tips_text:
        return False
    if "已完成" in tips_text:
        return True
    return _detect_completed_icon(point)


def _is_point_locked(point) -> bool:
    """判断章节是否需要解锁, 兼容文本提示与锁定图标两种形式。"""
    tips_text = _element_text(point.select_one("span.bntHoverTips"))
    if "解锁" in tips_text:
        return True
    for icon in point.find_all(["em", "i", "span"]):
        if "Lock" in _element_classes(icon):
            return True
    return False


def _extract_points_from_chapter(chapter_unit) -> List[Dict[str, Any]]:
    """
    从章节单元中提取章节点信息
    
    Args:
        chapter_unit: BeautifulSoup对象，表示一个章节单元
        
    Returns:
        章节点信息列表
    """
    point_list = []
    raw_points = chapter_unit.find_all("li")
    
    for raw_point in raw_points:
        point = raw_point.div
        if "id" not in point.attrs:
            continue
            
        point_id = re.findall(r"^cur(\d{1,20})$", point.attrs["id"])[0]
        point_title = point.select_one("a.clicktitle").text.replace("\n", "").strip()
        
        # 提取任务数量
        job_count = 1  # 默认为1
        need_unlock = False
        if point.select_one("input.knowledgeJobCount"):
            job_count = point.select_one("input.knowledgeJobCount").attrs["value"]
        elif _is_point_locked(point):
            need_unlock = True
            
        # 判断是否已完成
        is_finished = _is_point_finished(point)
            
        point_detail = {
            "id": point_id,
            "title": point_title,
            "jobCount": job_count,
            "has_finished": is_finished,
            "need_unlock": need_unlock
        }
        point_list.append(point_detail)
        
    return point_list


def decode_course_card(html_text: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    解析任务点列表页面，提取任务点信息
    
    Args:
        html_text: 任务点列表页面的HTML内容
        
    Returns:
        任务点列表和任务信息的元组
    """
    logger.trace("开始解码任务点列表...")
    
    # 检查章节是否未开放
    if "章节未开放" in html_text:
        return [], {"notOpen": True}

    # 提取mArg参数
    temp = re.findall(r"mArg=\{(.*?)\};", html_text.replace(" ", ""))
    if not temp:
        return [], {}

    # 解析JSON数据
    cards_data = json.loads("{" + temp[0] + "}")

    if not cards_data:
        return [], {}

    # 提取任务信息
    job_info = _extract_job_info(cards_data)

    # 处理所有附件任务
    cards = cards_data.get("attachments", [])
    job_list = _process_attachment_cards(cards)

    return job_list, job_info


def _extract_job_info(cards_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    从卡片数据中提取任务基本信息
    
    Args:
        cards_data: 卡片数据字典
        
    Returns:
        任务基本信息字典
    """
    defaults = cards_data.get("defaults", {})
    if not defaults:
        return {}
        
    return {
        "ktoken": defaults.get("ktoken", ""),
        "mtEnc": defaults.get("mtEnc", ""),
        "reportTimeInterval": defaults.get("reportTimeInterval", 60),
        "defenc": defaults.get("defenc", ""),
        "cardid": defaults.get("cardid", ""),
        "cpi": defaults.get("cpi", ""),
        "qnenc": defaults.get("qnenc", ""),
        "knowledgeid": defaults.get("knowledgeid", "")
    }


def _process_attachment_cards(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    处理所有附件任务卡片，强化直播任务识别逻辑
    
    Args:
        cards: 附件任务卡片列表
        
    Returns:
        处理后的任务列表
    """
    job_list = []
    
    for index, card in enumerate(cards):
        # 跳过已通过的任务
        if card.get("isPassed", False):
            continue

        # 处理无job字段的特殊任务
        if card.get("job") is None:
            # 尝试识别阅读任务
            read_job = _process_read_task(card)
            if read_job:
                job_list.append(read_job)
            continue

        # 一开始就把超星api的屎山处理掉，不要用一个屎山行为掩盖另一个屎山 (指根据otherInfo中是否有courseId决定url拼接方式😂)
        # 清理otherInfo字段中的无效参数，这里优化了一下(保留了作者原来的注释TAT）
        if "otherInfo" in card:
            logger.trace("Fixing other info...")
            card["otherInfo"] = card["otherInfo"].split("&")[0]
            logger.trace(f"New info: {card['otherInfo']}")

        # 多维度判断是否为直播任务
        card_type = card.get("type", "").lower()
        property_data = card.get("property", {})
        prop_type = property_data.get("type", "").lower()
        resource_type = property_data.get("resourceType", "").lower()
        
        # 直播任务特征：包含liveId、streamName等字段，
        # 或类型标识包含live（因为live和video有点类似，怕超星又搞出什么幺蛾子就加了一些关键字识别）
        is_live = (
            "live" in card_type 
            or "live" in prop_type
            or "live" in resource_type
            or "livestream" in card_type
            or property_data.get("liveId") is not None
            or property_data.get("streamName") is not None
            or property_data.get("vdoid") is not None
        )

        # 根据任务类型处理
        if is_live:
            live_job = _process_live_task(card)
            if live_job:
                job_list.append(live_job)
        elif card_type == "video":
            video_job = _process_video_task(card)
            if video_job:
                job_list.append(video_job)
        elif card_type == "document":
            doc_job = _process_document_task(card)
            if doc_job:
                job_list.append(doc_job)
        elif card_type == "workid":
            work_job = _process_work_task(card)
            if work_job:
                job_list.append(work_job)
        else:
            logger.warning(f"Unknown card type: {card_type}")
            logger.warning(card)

    return job_list


def _process_live_task(card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理直播类型任务，提取所有必要参数"""
    try:
        property_data = card.get("property", {})
        return {
            "type": "live",
            "jobid": card.get("jobid", str(card.get("id", ""))),  # 兼容不同格式的任务ID
            "name": property_data.get("title", property_data.get("name", "未知直播")),
            "otherinfo": card.get("otherInfo", ""),
            "property": property_data,  # 保留完整属性用于后续处理
            "mid": card.get("mid", ""),
            "objectid": card.get("objectId", ""),
            "aid": card.get("aid", ""),
            # 补充直播特有标识
            "liveId": property_data.get("liveId"),
            "streamName": property_data.get("streamName")
        }
    except Exception as e:
        logger.error(f"解析直播任务失败: {str(e)}, 任务数据: {str(card)[:200]}")
        return None
def _process_read_task(card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理阅读类型任务"""
    if not (card.get("type") == "read" and not card.get("property", {}).get("read", False)):
        return None
        
    return {
        "title": card.get("property", {}).get("title", ""),
        "type": "read",
        "id": card.get("property", {}).get("id", ""),
        "jobid": card.get("jobid", ""),
        "jtoken": card.get("jtoken", ""),
        "mid": card.get("mid", ""),
        "otherinfo": card.get("otherInfo", ""),
        "enc": card.get("enc", ""),
        "aid": card.get("aid", "")
    }


def _process_video_task(card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理视频类型任务"""
    try:
        return {
            "type": "video",
            "jobid": card.get("jobid", ""),
            "name": card.get("property", {}).get("name", ""),
            "otherinfo": card.get("otherInfo", ""),
            "mid": card["mid"],  # 必须字段，如果不存在会抛出异常
            "objectid": card.get("objectId", ""),
            "aid": card.get("aid", ""),
            "playTime": card.get("playTime", 0),
            "rt": card.get("property", {}).get("rt", ""),
            "attDuration": card.get("attDuration", ""),
            "attDurationEnc": card.get("attDurationEnc", ""),
            "videoFaceCaptureEnc": card.get("videoFaceCaptureEnc", ""),
        }
    except KeyError:
        logger.warning("出现转码失败视频，已跳过...")
        return None


def _process_document_task(card: Dict[str, Any]) -> Dict[str, Any]:
    """处理文档类型任务"""
    return {
        "type": "document",
        "jobid": card.get("jobid", ""),
        "otherinfo": card.get("otherInfo", ""),
        "jtoken": card.get("jtoken", ""),
        "mid": card.get("mid", ""),
        "enc": card.get("enc", ""),
        "aid": card.get("aid", ""),
        "objectid": card.get("property", {}).get("objectid", "")
    }


def _process_work_task(card: Dict[str, Any]) -> Dict[str, Any]:
    """处理作业类型任务"""
    return {
        "type": "workid",
        "jobid": card.get("jobid", ""),
        "otherinfo": card.get("otherInfo", ""),
        "mid": card.get("mid", ""),
        "enc": card.get("enc", ""),
        "aid": card.get("aid", "")
    }


def decode_questions_info(html_content: str) -> Dict[str, Any]:
    """
    解析题目信息，提取表单数据和问题列表
    
    Args:
        html_content: 题目页面HTML内容
        
    Returns:
        包含表单数据和问题列表的字典
    """
    soup = BeautifulSoup(html_content, "lxml")
    form_data = _extract_form_data(soup)
    
    # 检查是否存在字体加密
    has_font_encryption = bool(soup.find("style", id="cxSecretStyle"))
    font_decoder = None
    
    if has_font_encryption:
        font_decoder = FontDecoder(html_content)
    else:
        logger.warning("未找到字体文件，可能是未加密的题目不进行解密")
    
    # 处理所有问题
    questions = []
    for div_tag in soup.find("form").find_all("div", class_="singleQuesId"):
        question = _process_question(div_tag, font_decoder)
        if question:
            questions.append(question)
    
    # 更新表单数据
    form_data["questions"] = questions
    form_data["answerwqbid"] = ",".join([q["id"] for q in questions]) + ","
    
    return form_data


def _extract_form_data(soup: BeautifulSoup) -> Dict[str, Any]:
    """从BeautifulSoup对象中提取表单数据"""
    form_data = {}
    form_tag = soup.find("form")
    
    if not form_tag:
        return form_data
    
    # 提取所有非答案字段的input
    for input_tag in form_tag.find_all("input"):
        if "name" not in input_tag.attrs or "answer" in input_tag.attrs["name"]:
            continue
        form_data[input_tag.attrs["name"]] = input_tag.attrs.get("value", "")
    
    return form_data


def _process_question(div_tag, font_decoder=None) -> Dict[str, Any]:
    """处理单个问题"""
    # 提取问题ID和题目类型
    question_id = div_tag.attrs.get("data", "")
    q_type_code = div_tag.find("div", class_="TiMu").attrs.get("data", "")
    q_type = _get_question_type(q_type_code)
    
    # 提取题目内容和选项
    title_div = div_tag.find("div", class_="Zy_TItle")
    options_list = div_tag.find("ul").find_all("li") if div_tag.find("ul") else []
    
    # 解析题目和选项
    q_title = _extract_title(title_div, font_decoder)
    q_options = []
    for li in options_list:
        q_options.append(_extract_choices(li, font_decoder))
    # 排序选项
    q_options.sort()
    q_options = '\n'.join(q_options)
    
    return {
        "id": question_id,
        "title": q_title,
        "options": q_options,
        "type": q_type,
        "answerField": {
            f"answer{question_id}": "",
            f"answertype{question_id}": q_type_code,
        },
    }


def _get_question_type(type_code: str) -> str:
    """根据题型代码返回题型名称"""
    type_map = {
        "0": "single",      # 单选题
        "1": "multiple",    # 多选题
        "2": "completion",  # 填空题
        "3": "judgement",   # 判断题
        "4": "shortanswer", # 简答题
    }
    
    if type_code in type_map:
        return type_map[type_code]
    
    logger.info(f"未知题型代码 -> {type_code}")
    return "unknown"


def _extract_title(element, font_decoder=None) -> str:
    """提取标题内容，支持解码加密字体"""
    if not element:
        return ""
        
    # 收集元素中的所有文本和图片
    content = []
    for item in element.descendants:
        if isinstance(item, NavigableString):
            content.append(item.string or "")
        elif item.name == "img":
            img_url = item.get("src", "")
            content.append(f'<img src="{img_url}">')
    
    raw_content = "".join(content)
    cleaned_content = raw_content.replace("\r", "").replace("\t", "").replace("\n", "")
    
    # 如果有字体解码器，进行解码
    if font_decoder:
        return font_decoder.decode(cleaned_content)
    
    return cleaned_content

def _extract_choices(element, font_decoder=None) -> str:
    """提取选项内容，支持解码加密字体"""
    if not element:
        return ""
        
    # 提取aria-label属性值作为选项，解决#474
    choice = element.get("aria-label") or element.get_text()
    if not choice:
        return ""

    cleaned_content = re.sub(r"[\r\t\n]", "", choice)

    if font_decoder:
        cleaned_content = font_decoder.decode(cleaned_content)

    cleaned_content = cleaned_content.strip()
    if cleaned_content.endswith("选择"):
        cleaned_content = cleaned_content[:-2].rstrip()

    return cleaned_content


def decode_homework_form(html_content: str) -> Dict[str, Any]:
    """
    解析独立课程作业(作业中心)的答题页, 提取提交所需表单数据与题目列表

    与章节检测(decode_questions_info)不同, 作业中心答题页结构差异较大:
    - 题目节点: div.singleQuesId 的 data 属性即题目id
    - 标题: h3.mark_name(含题型提示span需清理)
    - 选项: div.answerBg, span[data]为选项值, div.answer_p>p为选项文本
    - 填空/简答: textarea#answer{id}
    - 提交: form action 指向 /mooc-ans/work/addStudentWorkNewWeb
      (query含 classId/courseid/token/totalQuestionNum/wMicroNodeId)
    - hidden input: standardEnc/enc_work/totalQuestionNum/workRelationId/workAnswerId/mooc2/randomOptions/workTimesEnc

    Returns:
        包含表单数据与题目列表的字典, 供 homework 模块提交使用
    """
    soup = BeautifulSoup(html_content, "lxml")

    form_tag = soup.find("form", id="submitForm") or soup.find("form")
    form_data = {}

    if form_tag:
        # 从 form action 提取参数(_classId/courseid/token/totalQuestionNum/wMicroNodeId)
        action = form_tag.attrs.get("action", "")
        m = re.search(r"addStudentWorkNewWeb\?", action)
        if m:
            query = action[m.end():]
            for k, v in re.findall(r"([^=&?]+)=([^&]*)", query):
                form_data.setdefault(k, v)

        # 提取表单内所有 hidden input
        for input_tag in form_tag.find_all("input", {"type": "hidden"}):
            name = input_tag.attrs.get("name") or input_tag.attrs.get("id")
            if name:
                form_data.setdefault(name, input_tag.attrs.get("value", ""))

    # 与章节检测一致, 从页面级 hidden input 兜底补充(部分字段可能不在form内)
    for key in ("standardEnc", "enc_work", "totalQuestionNum", "workRelationId", "workAnswerId", "mooc2", "randomOptions", "workTimesEnc"):
        if key not in form_data:
            tag = soup.find("input", {"name": key}) or soup.find("input", {"id": key})
            if tag:
                form_data[key] = tag.attrs.get("value", "")

    has_font_encryption = bool(soup.find("style", id="cxSecretStyle"))
    font_decoder = FontDecoder(html_content) if has_font_encryption else None
    if not has_font_encryption:
        logger.warning("未找到字体文件，可能是未加密的题目不进行解密")

    questions = []
    for div_tag in soup.find_all("div", class_="singleQuesId"):
        question = _process_homework_question(div_tag, font_decoder)
        if question:
            questions.append(question)

    form_data["questions"] = questions
    form_data["answerwqbid"] = ",".join([q["id"] for q in questions]) + ","

    return form_data


def _process_homework_question(div_tag, font_decoder=None) -> Dict[str, Any]:
    """处理单个作业题目"""
    question_id = div_tag.attrs.get("data", "")

    # 题型码: answertype{id} hidden input
    q_type_code = ""
    type_input = div_tag.find("input", attrs={"name": f"answertype{question_id}"})
    if type_input is not None:
        q_type_code = type_input.attrs.get("value", "")
    # 兜底: 从 typeName 属性映射
    if not q_type_code:
        type_name = div_tag.attrs.get("typeName", "")
        q_type_code = _homework_type_name_to_code(type_name)

    q_type = _get_question_type(q_type_code)

    # 题目标题 h3.mark_name(清理数字序号与题型提示)
    title_div = div_tag.find("h3", class_="mark_name")
    q_title = _extract_homework_title(title_div, font_decoder)

    # 选项: div.answerBg, span[data]为值, div.answer_p>p为文本
    options_list = []
    answer_map = {}
    for answer_div in div_tag.find_all("div", class_="answerBg"):
        opt_value = ""
        opt_span = answer_div.find("span", attrs={"data": True})
        if opt_span is not None:
            opt_value = opt_span.attrs.get("data", "")
        if not opt_value:
            continue
        opt_text_p = answer_div.find("div", class_="answer_p")
        opt_text = ""
        if opt_text_p is not None:
            p = opt_text_p.find("p")
            if p is not None:
                opt_text = p.get_text()
        if font_decoder:
            opt_text = font_decoder.decode(opt_text)
        opt_text = opt_text.strip()
        line = f"{opt_value} {opt_text}" if opt_text else opt_value
        options_list.append(line)
        answer_map[opt_value] = opt_text

    options_str = "\n".join(options_list)

    # 已填答案(仅用于参考/判断是否已做, 交由 homework 模块处理)
    existing_answer = ""
    answer_input = div_tag.find("input", attrs={"name": f"answer{question_id}"})
    if answer_input is not None:
        existing_answer = answer_input.attrs.get("value", "")

    return {
        "id": question_id,
        "title": q_title,
        "options": options_str,
        "type": q_type,
        "type_code": q_type_code,
        "existing_answer": existing_answer,
        "answer_map": answer_map,
        "answerField": {
            f"answer{question_id}": "",
            f"answertype{question_id}": q_type_code,
        },
    }


def _homework_type_name_to_code(type_name: str) -> str:
    """将作业页 typeName 属性映射为题型码"""
    type_map = {
        "单选题": "0",
        "多选题": "1",
        "填空题": "2",
        "判断题": "3",
        "简答题": "4",
    }
    return type_map.get(type_name, "")


def _extract_homework_title(element, font_decoder=None) -> str:
    """提取作业题目标题, 清理数字序号与(题型)提示"""
    if not element:
        return ""

    content = []
    for item in element.descendants:
        if isinstance(item, NavigableString):
            content.append(item.string or "")
        elif item.name == "img":
            img_url = item.get("src", "")
            content.append(f'<img src="{img_url}">')

    raw_content = "".join(content)
    raw_content = raw_content.replace("\r", "").replace("\t", "").replace("\n", "")

    # 清理开头数字序号, 如 "1. " / "267. "
    raw_content = re.sub(r'^\s*\d+\s*[\.、．]?\s*', '', raw_content)
    # 清理题型提示 (单选题) 等
    raw_content = re.sub(r'\(\s*[^)]*(?:题|填空)[^)]*\)', '', raw_content)

    if font_decoder:
        raw_content = font_decoder.decode(raw_content)

    return raw_content.strip()
