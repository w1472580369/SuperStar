# -*- coding: utf-8 -*-
"""
独立课程作业(作业中心)答题模块

与章节检测(study_work)不同, 作业中心的作业独立于章节任务点, 通过课程页面
"作业"导航进入, 需要独立的列表/详情/提交接口。

接口来源(用户抓包自 mooc2-ans.chaoxing.com.har / mooc1.chaoxing.com1.har):
1. 作业列表: GET /mooc-ans/mooc2/work/list
   ?courseid=&clazzid=&cpi=&ut=s&t=<ms>&stuenc=<stuEnc>&enc=<workEnc>
   - stuEnc/homeworkEnc 取自 stu(studentcourse)页面的 #enc / #workEnc hidden input
   - 列表项 <li data=".../mooc2/work/task?workId=&answerId=&enc=">
2. 作业详情: GET <li.data>(/mooc-ans/mooc2/work/task)
   - HTML 含题目节点 singleQuesId, form(action=addStudentWorkNewWeb)及 hidden 参数
3. 保存/提交: POST /mooc-ans/work/addStudentWorkNewWeb
   ?_classId=&courseid=&token=&totalQuestionNum=&wMicroNodeId=&pyFlag=&ua=pc&formType=post&saveStatus=&version=1
   - pyFlag: 1=保存, 空=提交
"""
import re
import time

from loguru import logger

from api.answer import Tiku
from api.base import SessionManager, StudyResult, get_timestamp
from api.decode import decode_homework_form


class HomeworkError(Exception):
    """作业处理异常"""


def _get_stu_encs(course: dict) -> dict:
    """
    获取课程页面中的 workEnc 与 stuEnc(#enc)。

    作业列表接口需要这两个加密参数。新版学习通把它们放在课程首页
    (mycourse/stu), 旧版放在章节页(studentcourse)。进入课程页面需要
    课程加密参数 enc, 该值来自课程列表卡片 info 属性或课程链接的 enc 参数。
    """
    _session = SessionManager.get_session()
    course_enc = course.get("enc") or course.get("info") or ""
    _openc = course.get("openc", "")

    # 与浏览器抓包一致, 带上 Referer 等导航请求头, 否则超星可能返回不含表单的页面
    _stu_headers = {
        "Referer": "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction"
                   "?moocDomain=https://mooc1-1.chaoxing.com/mooc-ans",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Upgrade-Insecure-Requests": "1",
    }

    # 依次尝试各类课程页面, 直至解析到 workEnc/stuEnc
    candidates = []
    if course_enc:
        candidates.append(
            f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?"
            f"courseid={course['courseId']}&clazzid={course['clazzId']}"
            f"&cpi={course['cpi']}&enc={course_enc}"
            f"&t={get_timestamp()}&pageHeader=8&v=2&hideHead=0"
        )
        if _openc:
            candidates.append(
                f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?"
                f"courseid={course['courseId']}&clazzid={course['clazzId']}"
                f"&cpi={course['cpi']}&enc={course_enc}&openc={_openc}&fromMiddle=1"
            )
    else:
        logger.warning("课程缺少 enc(info) 参数, 将尝试不带 enc 的地址")
    candidates.append(
        f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?"
        f"courseid={course['courseId']}&clazzid={course['clazzId']}"
        f"&cpi={course['cpi']}&t={get_timestamp()}&pageHeader=8&v=2&hideHead=0"
    )
    candidates.append(
        f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?"
        f"courseid={course['courseId']}&clazzid={course['clazzId']}"
        f"&cpi={course['cpi']}&ut=s"
    )

    last_status = None
    for _url in candidates:
        logger.info(f"尝试获取课程作业加密参数 -> course={course['courseId']} enc={course_enc or '(空)'}")
        logger.info("URL: " + _url)
        _resp = _session.get(_url, headers=_stu_headers)
        last_status = _resp.status_code
        if _resp.status_code != 200:
            logger.warning(f"获取课程页面失败 -> [{_resp.status_code}]{_resp.text[:200]}")
            continue
        work_enc = _extract_hidden(_resp.text, "id", "workEnc")
        stu_enc = _extract_hidden(_resp.text, "id", "enc")
        if work_enc and stu_enc:
            logger.info(f"workEnc={work_enc} stuEnc={stu_enc}")
            return {"workEnc": work_enc, "stuEnc": stu_enc}
        has_enc_tag = ('id="enc"' in _resp.text) or ('name="enc"' in _resp.text)
        logger.warning(
            f"课程页面未解析到 workEnc/stuEnc(页面长度 {len(_resp.text)}, "
            f"含enc标记 {has_enc_tag}, 含workEnc标记 {'workEnc' in _resp.text}), 尝试下一个地址"
        )

    logger.error(f"获取课程作业加密参数失败(最后状态码 {last_status})")
    raise HomeworkError("获取课程作业加密参数失败")


def _extract_hidden(html_text: str, id_or_name: str, key: str) -> str:
    """从 HTML 中提取 hidden input 的 value(兼容 id/name 精确匹配, 不限定 type=hidden)"""
    # 1) 精确匹配 id="{key}"
    m = re.search(
        rf'<input\b[^>]*?\bid\s*=\s*["\']{re.escape(key)}["\'][^>]*?>',
        html_text,
        re.IGNORECASE,
    )
    # 2) 精确匹配 name="{key}"
    if not m:
        m = re.search(
            rf'<input\b[^>]*?\bname\s*=\s*["\']{re.escape(key)}["\'][^>]*?>',
            html_text,
            re.IGNORECASE,
        )
    # 3) 兼容 type=hidden 与 key 任一顺序
    if not m:
        m = re.search(
            rf'<input\b[^>]*?(?:type\s*=\s*["\']hidden["\'])[^>]*?\b{key}\b[^>]*?>',
            html_text,
            re.IGNORECASE,
        )
    if not m:
        m = re.search(
            rf'<input\b[^>]*?\b{key}\b[^>]*?(?:type\s*=\s*["\']hidden["\'])[^>]*?>',
            html_text,
            re.IGNORECASE,
        )
    if not m:
        return ""
    value_m = re.search(r'value\s*=\s*["\']([^"\']*)["\']', m.group(0), re.IGNORECASE)
    return value_m.group(1) if value_m else ""


def get_homework_list(course: dict, status: int = 1) -> list[dict]:
    """
    获取课程作业列表。

    status: 0=全部, 1=未完成, 2=已完成(列表页筛选Tab)
    """
    encs = _get_stu_encs(course)
    _session = SessionManager.get_session()
    _url = "https://mooc1.chaoxing.com/mooc2/work/list"
    _headers = {
        "Referer": (
            "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?"
            f"courseid={course['courseId']}&clazzid={course['clazzId']}"
            f"&cpi={course['cpi']}&enc={encs['stuEnc']}"
            f"&t={get_timestamp()}&pageHeader=8&v=2&hideHead=0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Upgrade-Insecure-Requests": "1",
    }
    params = {
        "courseId": course["courseId"],
        "classId": course["clazzId"],
        "cpi": course["cpi"],
        "ut": "s",
        "t": get_timestamp(),
        "stuenc": encs["stuEnc"],
        "enc": encs["workEnc"],
        "status": status,
    }
    logger.trace("URL: " + _url)
    _resp = _session.get(_url, params=params, headers=_headers)
    if _resp.status_code != 200:
        logger.error(f"获取作业列表失败 -> [{_resp.status_code}]{_resp.text[:200]}")
        raise HomeworkError("获取作业列表失败")

    return _parse_homework_list(_resp.text)


def _parse_homework_list(html_text: str) -> list[dict]:
    """解析作业列表 HTML, 提取每个作业的进入链接(含 workId/answerId/enc)"""
    works = []
    # 作业条目 <li data="https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?...">
    for m in re.finditer(r'<li\b[^>]*\bdata\s*=\s*["\']([^"\']*(?:/mooc2/work/task|/mooc-ans/mooc2/work/task)[^"\']*)["\']', html_text):
        url = m.group(1).replace("&amp;", "&")
        work = {"url": url}
        for key in ("courseId", "classId", "cpi", "workId", "answerId", "enc"):
            value = re.search(rf"(?:^|[?&]){key}=([^&]*)", url)
            if value:
                work[key] = value.group(1)
        works.append(work)

    if not works:
        # 兼容 aria-label 位于 data 前或属性顺序不同的情况
        for m in re.finditer(
            r'<li\b[^>]*?\bdata\s*=\s*["\']([^"\']*)["\']',
            html_text,
        ):
            url = m.group(1).replace("&amp;", "&")
            if "/work/" not in url:
                continue
            work = {"url": url}
            for key in ("courseId", "classId", "cpi", "workId", "answerId", "enc"):
                value = re.search(rf"(?:^|[?&]){key}=([^&]*)", url)
                if value:
                    work[key] = value.group(1)
            works.append(work)

    return works


def _is_homework_form(html_text: str) -> bool:
    """判断页面是否为作业答题页(含题目表单)"""
    return ("singleQuesId" in html_text) or ("addStudentWorkNewWeb" in html_text)


def _extract_dowork_url(html_text: str, work: dict) -> str:
    """
    从 task 跳转页中提取真正的答题页(dowork)地址。

    兼容 iframe / meta refresh / JS 跳转三种写法。
    """
    patterns = (
        r'(?:src|href)\s*=\s*["\']([^"\']*/mooc2/work/dowork[^"\']*)["\']',
        r'["\'](https?://[^"\']*/mooc2/work/dowork[^"\']*)["\']',
        r'(/mooc-ans/mooc2/work/dowork\?[^"\'\s<>\\]*)',
    )
    for pattern in patterns:
        m = re.search(pattern, html_text, re.IGNORECASE)
        if m:
            return m.group(1).replace("&amp;", "&")

    # 兜底: 依据列表项参数自行拼接 dowork 地址
    if work.get("workId"):
        return (
            "https://mooc1.chaoxing.com/mooc-ans/mooc2/work/dowork?"
            f"courseId={work.get('courseId', '')}&classId={work.get('classId', '')}"
            f"&cpi={work.get('cpi', '')}&workId={work.get('workId', '')}"
            f"&answerId={work.get('answerId', '')}&enc={work.get('enc', '')}"
        )
    return ""


def get_homework_detail(work: dict) -> dict:
    """获取作业详情页(题目页), 并解析表单与题目"""
    _session = SessionManager.get_session()
    _url = work["url"]
    _referer = "https://mooc1.chaoxing.com/mooc2/work/list"
    _headers = {
        "Referer": _referer,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Upgrade-Insecure-Requests": "1",
    }
    logger.trace("URL: " + _url)
    _resp = _session.get(_url, headers=_headers)
    if _resp.status_code != 200:
        logger.error(f"获取作业详情失败 -> [{_resp.status_code}]{_resp.text[:200]}")
        raise HomeworkError("获取作业详情失败")

    # task 页面可能只是跳转页, 真正题目在 dowork 页面
    if not _is_homework_form(_resp.text):
        _dowork_url = _extract_dowork_url(_resp.text, work)
        if _dowork_url:
            logger.trace("URL: " + _dowork_url)
            _resp = _session.get(_dowork_url, headers=_headers)
            if _resp.status_code != 200:
                logger.error(f"获取作业答题页失败 -> [{_resp.status_code}]{_resp.text[:200]}")
                raise HomeworkError("获取作业答题页失败")

    return decode_homework_form(_resp.text)


def solve_homework(tiku: Tiku, form: dict, query_delay: float = 0.0) -> tuple[int, int]:
    """
    对作业题目逐题搜题并填写答案。

    Returns:
        (找到答案的题目数, 总题数)
    """
    questions = form.get("questions", [])
    total_questions = len(questions)
    found_answers = 0

    for q in questions:
        logger.debug(f"当前题目信息 -> {q}")
        if query_delay > 0:
            time.sleep(query_delay)

        # 使用题库搜索
        res = tiku.query(q)
        answer = ""

        if not res:
            logger.warning(f"未找到答案 -> {q['title']}")
        else:
            if q["type"] == "multiple":
                # 多选: 从答案中匹配选项值(A/B/C...)
                answer = _match_options(res, q)
            elif q["type"] == "single":
                answer = _match_options(res, q)
            elif q["type"] == "judgement":
                answer = _match_judgement(res, q)
            elif q["type"] == "completion":
                answer = res.strip()
            else:
                # 简答等直接使用答案
                answer = res.strip()

            if answer:
                found_answers += 1

        q["answerField"][f"answer{q['id']}"] = answer
        logger.info(f'{q["title"]} 填写答案为 {answer or "(空)"}')

    return found_answers, total_questions


def _match_options(res, q):
    """
    从搜题结果中匹配选项值(字母 A/B/C...), 类似章节检测的选项匹配逻辑。

    优先处理两种形态:
    1. 答案直接含选项字母(如 "答案: A" / "ABCD")
    2. 答案是选项文本本身(如 "答案: 第一次鸦片战争"), 需回映射到对应选项字母
    """
    # 解析选项: "A 團" -> (A, 團)
    option_pairs = []
    for line in q["options"].split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        letter = parts[0].strip()
        text = parts[1].strip() if len(parts) > 1 else ""
        if letter:
            option_pairs.append((letter, text))

    if not option_pairs:
        return ""

    letters = [p[0] for p in option_pairs]

    # 去掉前缀 "答案/正确答案/选*" 等 (只吃标点和空白, 不吃中文)
    cleaned = re.sub(r"^(?:答案|正确答案|选|应选|正确选项)[：:\s]*", "", res.strip())
    # 判断题选项可能是 true/false
    cleaned_lower = cleaned.lower()

    # 1) 直接字母序列匹配(如 "A" / "AB" / "A、C")
    letter_answer = ""
    for token in re.findall(r"[A-Za-z]+", cleaned):
        for ch in token:
            upper = ch.upper()
            if upper in letters and upper not in letter_answer:
                letter_answer += upper
    if letter_answer:
        return letter_answer

    # 2) 文本匹配: 答案文本对应某个选项文本
    answer_norm = re.sub(r"\s+", "", cleaned)
    # 精确匹配
    for letter, text in option_pairs:
        if text and answer_norm == re.sub(r"\s+", "", text):
            return letter
    # 包含匹配(答案文本是选项文本的子串或反之), 优先最长选项
    contained = []
    for letter, text in option_pairs:
        if not text or not answer_norm:
            continue
        text_norm = re.sub(r"\s+", "", text)
        if answer_norm in text_norm or text_norm in answer_norm:
            contained.append((len(text_norm), letter))
    if contained:
        contained.sort(key=lambda x: -x[0])
        return contained[0][1]

    # 3) 判断题 true/false
    true_keys = ["正确", "对", "√", "是"]
    false_keys = ["错误", "错", "×", "否", "不对", "不正确"]
    for key in false_keys:
        if key in cleaned_lower:
            return "false"
    for key in true_keys:
        if key in cleaned_lower:
            return "true"

    return ""


def _match_judgement(res, q):
    """
    匹配判断题答案: 复用 tiku 配置的 true_list/false_list 做包含匹配。
    返回 'true'/'false'/''(无法判断时留空, 不随机)。
    """
    try:
        from api.answer import Tiku
        sample = Tiku()
        if sample._conf:
            true_keys = sample.true_list
            false_keys = sample.false_list
        else:
            true_keys = ["正确", "对", "√", "是"]
            false_keys = ["错误", "错", "×", "否", "不对", "不正确"]
    except Exception:
        true_keys = ["正确", "对", "√", "是"]
        false_keys = ["错误", "错", "×", "否", "不对", "不正确"]

    normalized = re.sub(r"\s+", "", res)
    # 优先匹配否定(错误)关键词, 避免"对"出现在"不对"中导致误判
    for key in false_keys:
        if key and key in normalized:
            return "false"
    for key in true_keys:
        if key and key in normalized:
            return "true"
    return ""


def submit_homework(course: dict, work: dict, form: dict, submit: bool) -> StudyResult:
    """
    保存或提交作业。

    submit=True 提交(pyFlag=''), False 仅保存(pyFlag='1')
    """
    _session = SessionManager.get_session()

    base_query = {
        "_classId": course["clazzId"],
        "courseid": course["courseId"],
        "token": form.get("enc_work", ""),
        "totalQuestionNum": form.get("totalQuestionNum", ""),
        "wMicroNodeId": "",
    }
    submit_params = {
        "pyFlag": "" if submit else "1",
        "ua": "pc",
        "formType": "post",
        "saveStatus": "0" if submit else "1",
        "version": "1",
    }
    url_query = {**base_query, **submit_params}
    url = "https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNewWeb?" + "&".join(
        f"{k}={v}" for k, v in url_query.items()
    )

    # 组装 body
    body = {
        "courseId": course["courseId"],
        "classId": course["clazzId"],
        "knowledgeid": "0",
        "cpi": course["cpi"],
        "workRelationId": form.get("workRelationId", "") or work.get("workId", ""),
        "workAnswerId": form.get("workAnswerId", "") or work.get("answerId", ""),
        "jobid": "",
        "standardEnc": form.get("standardEnc", ""),
        "enc_work": form.get("enc_work", ""),
        "totalQuestionNum": form.get("totalQuestionNum", ""),
        "pyFlag": "" if submit else "1",
        "mooc2": form.get("mooc2", "1"),
        "randomOptions": form.get("randomOptions", "false"),
        "workTimesEnc": form.get("workTimesEnc", ""),
        "answerwqbid": form.get("answerwqbid", ""),
    }

    for q in form.get("questions", []):
        bid = q["id"]
        body[f"answer{bid}"] = q["answerField"].get(f"answer{bid}", "")
        body[f"answertype{bid}"] = q["answerField"].get(f"answertype{bid}", q.get("type_code", ""))

    logger.info(f'{"提交" if submit else "保存"}作业: {work.get("url", "")}')
    logger.trace("URL: " + url)
    _resp = _session.post(
        url,
        data=body,
        headers={
            "Host": "mooc1.chaoxing.com",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://mooc1.chaoxing.com",
            "Referer": work.get("url", ""),
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    if _resp.status_code != 200:
        logger.error(f'{"提交" if submit else "保存"}作业失败 -> [{_resp.status_code}]{_resp.text[:200]}')
        return StudyResult.ERROR

    try:
        res_json = _resp.json()
    except ValueError:
        logger.error(f'{"提交" if submit else "保存"}作业失败 -> 返回非JSON: {_resp.text[:200]}')
        return StudyResult.ERROR

    if res_json.get("status"):
        logger.info(f'{"提交" if submit else "保存"}作业成功 -> {res_json.get("msg", "")}')
        return StudyResult.SUCCESS

    logger.error(f'{"提交" if submit else "保存"}作业失败 -> {res_json.get("msg", "")}')
    return StudyResult.ERROR