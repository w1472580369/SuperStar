# -*- coding: utf-8 -*-
import argparse
import configparser
import enum
import sys
import threading
import time
import traceback
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import dataclass, field
from queue import PriorityQueue
from threading import RLock
from typing import Any

from tqdm import tqdm

from api.answer import Tiku
from api.base import Chaoxing, Account, StudyResult
from api.exceptions import LoginError, InputFormatError
from api.homework import (
    HomeworkError,
    get_homework_detail,
    get_homework_list,
    solve_homework,
    submit_homework,
)
from api.homework_pdf import generate_homework_pdf
from api.logger import configure_console_logger, logger
from api.notification import Notification
from api.live import Live
from api.live_process import LiveProcessor

class ChapterResult(enum.Enum):
    SUCCESS=0,
    ERROR=1,
    NOT_OPEN=2,
    PENDING=3


def log_error(func):
    def wrapper(*args, **kwargs):
        try:
            func(*args, **kwargs)
        except BaseException as e:
            logger.error(f"Error in thread {threading.current_thread().name}: {e}")
            traceback.print_exception(type(e), e, e.__traceback__)
            raise

    return wrapper


def str_to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Samueli924/chaoxing",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("--use-cookies", action="store_true", help="使用cookies登录")

    parser.add_argument(
        "-c", "--config", type=str, default=None, help="使用配置文件运行程序"
    )
    parser.add_argument("-u", "--username", type=str, default=None, help="手机号账号")
    parser.add_argument("-p", "--password", type=str, default=None, help="登录密码")
    parser.add_argument(
        "-l", "--list", type=str, default=None, help="要学习的课程ID列表, 以 , 分隔"
    )
    parser.add_argument(
        "-s", "--speed", type=float, default=1.0, help="视频播放倍速 (默认1, 最大2)"
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=1, help="同时进行的章节数 (默认1=串行刷课, 降低被判定多设备并发刷课的风险; 如需加速可调大)"
    )

    parser.add_argument(
        "-v",
        "--verbose",
        "--debug",
        action="store_true",
        help="启用调试模式, 输出DEBUG级别日志",
    )
    parser.add_argument(
        "-a", "--notopen-action", type=str, default="retry", 
        choices=["retry", "ask", "continue"],
        help="遇到关闭任务点时的行为: retry-重试, ask-询问, continue-继续"
    )

    parser.add_argument("--auto-sign", action="store_true", help="自动签到")

    # 独立作业答题(作业中心)
    parser.add_argument(
        "--homework", action="store_true",
        help="启用作业答题(独立课程作业, 非章节检测)",
    )
    parser.add_argument(
        "--homework-submit", type=str, default=None,
        choices=["true", "false"],
        help="作业是否提交: true=提交(pyFlag空), false=仅保存(pyFlag=1)。默认读 config.ini 的 homework.submit",
    )
    parser.add_argument(
        "--homework-cover-rate", type=float, default=None,
        help="作业最低搜题覆盖率, 达到才提交, 否则仅保存。默认读 config.ini 的 homework.cover_rate",
    )
    parser.add_argument(
        "--homework-only", action="store_true",
        help="仅运行作业答题, 不学习章节任务点",
    )

    # 在解析之前捕获 -h 的行为
    if len(sys.argv) == 2 and sys.argv[1] in {"-h", "--help"}:
        parser.print_help()
        sys.exit(0)

    return parser.parse_args()


def load_config_from_file(config_path):
    """从配置文件加载设置"""
    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf8")
    
    common_config: dict[str, Any] = {}
    tiku_config: dict[str, Any] = {}
    notification_config: dict[str, Any] = {}
    homework_config: dict[str, Any] = {}
    
    # 检查并读取common节
    if config.has_section("common"):
        common_config = dict(config.items("common"))
        # 处理course_list，将字符串转换为列表
        if "course_list" in common_config and common_config["course_list"]:
            common_config["course_list"] = [item.strip() for item in common_config["course_list"].split(",") if item.strip()]
        # 处理speed，将字符串转换为浮点数
        if "speed" in common_config:
            common_config["speed"] = float(common_config["speed"])
        if "jobs" in common_config:
            common_config["jobs"] = int(common_config["jobs"])
        # 处理notopen_action，设置默认值为retry
        if "notopen_action" not in common_config:
            common_config["notopen_action"] = "retry"
        if "study" in common_config:
            common_config["study"] = str_to_bool(common_config["study"])
        if "use_cookies" in common_config:
            common_config["use_cookies"] = str_to_bool(common_config["use_cookies"])
        if "username" in common_config and common_config["username"] is not None:
            common_config["username"] = common_config["username"].strip()
        if "password" in common_config and common_config["password"] is not None:
            common_config["password"] = common_config["password"].strip()
        if "verbose" in common_config:
            common_config["verbose"] = str_to_bool(common_config["verbose"])

    # 检查并读取tiku节
    if config.has_section("tiku"):
        tiku_config = dict(config.items("tiku"))
        # 处理数值类型转换
        for key in ["delay", "cover_rate"]:
            if key in tiku_config:
                tiku_config[key] = float(tiku_config[key])

    # 检查并读取homework节(独立作业答题)
    if config.has_section("homework"):
        homework_config = dict(config.items("homework"))
        if "enable" in homework_config:
            homework_config["enable"] = str_to_bool(homework_config["enable"])
        if "pdf" in homework_config:
            homework_config["pdf"] = str_to_bool(homework_config["pdf"])
        if "submit" in homework_config:
            homework_config["submit"] = str_to_bool(homework_config["submit"])
        if "cover_rate" in homework_config:
            homework_config["cover_rate"] = float(homework_config["cover_rate"])
        if "delay" in homework_config:
            homework_config["delay"] = float(homework_config["delay"])

    # 检查并读取notification节
    if config.has_section("notification"):
        notification_config = dict(config.items("notification"))
    
    return common_config, tiku_config, notification_config, homework_config


def build_config_from_args(args):
    """从命令行参数构建配置"""
    common_config = {
        "use_cookies": args.use_cookies,
        "username": args.username,
        "password": args.password,
        "course_list": [item.strip() for item in args.list.split(",") if item.strip()] if args.list else None,
        "speed": args.speed if args.speed else 1.0,
        "jobs": args.jobs,
        "notopen_action": args.notopen_action if args.notopen_action else "retry",
        "verbose": args.verbose,
    }
    return common_config, {}, {}, {}


def init_config():
    """初始化配置"""
    args = parse_args()

    if args.config:
        common_config, tiku_config, notification_config, homework_config = load_config_from_file(args.config)
        if args.verbose:
            common_config["verbose"] = True
        # CLI 参数覆盖配置
        if args.homework:
            homework_config["enable"] = True
        if args.homework_submit is not None:
            homework_config["submit"] = args.homework_submit == "true"
        if args.homework_cover_rate is not None:
            homework_config["cover_rate"] = args.homework_cover_rate
        if args.homework_only:
            homework_config["enable"] = True
            homework_config["only"] = True
        return common_config, tiku_config, notification_config, homework_config, args
    else:
        common_config, tiku_config, notification_config, homework_config = build_config_from_args(args)
        # 无 -c 时尝试从默认配置文件(工作目录 config.ini)读取 common.study 与 homework 节, 与题库行为一致
        try:
            _parser = configparser.ConfigParser()
            _parser.read("config.ini", encoding="utf8")
            if _parser.has_section("common") and "study" in dict(_parser.items("common")):
                common_config["study"] = str_to_bool(_parser.get("common", "study"))
            if _parser.has_section("homework"):
                _cfg = dict(_parser.items("homework"))
                if "enable" in _cfg:
                    _cfg["enable"] = str_to_bool(_cfg["enable"])
                if "pdf" in _cfg:
                    _cfg["pdf"] = str_to_bool(_cfg["pdf"])
                if "submit" in _cfg:
                    _cfg["submit"] = str_to_bool(_cfg["submit"])
                if "cover_rate" in _cfg:
                    _cfg["cover_rate"] = float(_cfg["cover_rate"])
                if "delay" in _cfg:
                    _cfg["delay"] = float(_cfg["delay"])
                homework_config.update(_cfg)
        except Exception:
            pass
        # CLI 参数覆盖配置
        if args.homework:
            homework_config["enable"] = True
        if args.homework_submit is not None:
            homework_config["submit"] = args.homework_submit == "true"
        if args.homework_cover_rate is not None:
            homework_config["cover_rate"] = args.homework_cover_rate
        if args.homework_only:
            homework_config["enable"] = True
            homework_config["only"] = True
        return common_config, tiku_config, notification_config, homework_config, args



def init_chaoxing(common_config, tiku_config, config_path=None):
    """初始化超星实例"""
    username = common_config.get("username", "")
    password = common_config.get("password", "")
    use_cookies = common_config.get("use_cookies", False)
    
    # 如果没有提供用户名密码，从命令行获取
    if (not username or not password) and not use_cookies:
        username = input("请输入你的手机号, 按回车确认\n手机号:")
        password = input("请输入你的密码, 按回车确认\n密码:")
    
    account = Account(username, password)
    
    # 设置题库
    tiku = Tiku()
    tiku.config_set(tiku_config)  # 载入配置
    if config_path:
        tiku.set_config_path(config_path)  # 让 -c 指定的配置文件对题库生效
    tiku = tiku.get_tiku_from_config()  # 载入题库
    tiku.init_tiku()  # 初始化题库
    
    # 获取查询延迟设置
    
    # 检查大模型连接（如果使用的是大模型题库）
    # 使用实际加载后的题库配置判断, 兼容仅 CLI 参数(无 -c)时读取 config.ini 的情况
    loaded_config = tiku._conf or {}
    provider = loaded_config.get('provider', '')
    provider_list = [name.strip() for name in provider.split(',') if name.strip()]
    if any(name in ['AI', 'DeepSeekWeb2API', 'SiliconFlow'] for name in provider_list):
        check_connection = loaded_config.get('check_llm_connection', 'true').lower() == 'true'
        if check_connection:
            logger.info(f'正在验证大模型配置 (provider={provider})...')
            if not tiku.check_llm_connection():
                logger.error('大模型连接检查失败')
                choice = ''
                # CI/无交互环境下不阻塞, 直接默认继续(与直接回车一致)
                if sys.stdin.isatty():
                    choice = input('大模型连接检查失败，无法准确答题，是否继续运行？(Y/n): ').strip().lower()
                else:
                    logger.warning('非交互环境, 默认继续运行')
                # 直接回车默认继续运行
                if choice not in ('', 'y', 'yes'):
                    raise RuntimeError('用户取消运行')
                logger.info('用户选择继续运行...')

    query_delay = tiku_config.get("delay", 0)
    
    # 实例化超星API
    chaoxing = Chaoxing(account=account, tiku=tiku, query_delay=query_delay)
    
    return chaoxing

def process_job(chaoxing: Chaoxing, course: dict, job: dict, job_info: dict, speed: float) -> StudyResult:
    """处理单个任务点"""
    # 视频任务
    if job["type"] == "video":
        logger.trace(f"识别到视频任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        # 超星的接口没有返回当前任务是否为Audio音频任务
        video_result = chaoxing.study_video(
            course, job, job_info, _speed=speed, _type="Video"
        )
        if video_result.is_failure():
            logger.warning("当前任务非视频任务, 正在尝试音频任务解码")
            video_result = chaoxing.study_video(
                course, job, job_info, _speed=speed, _type="Audio")
        if video_result.is_failure():
            logger.warning(
                f"出现异常任务 -> 任务章节: {course['title']} 任务ID: {job['jobid']}, 已跳过"
            )
        return video_result
    # 文档任务
    elif job["type"] == "document":
        logger.trace(f"识别到文档任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        return chaoxing.study_document(course, job)
    # 测验任务
    elif job["type"] == "workid":
        logger.trace(f"识别到章节检测任务, 任务章节: {course['title']}")
        return chaoxing.study_work(course, job, job_info)
    # 阅读任务
    elif job["type"] == "read":
        logger.trace(f"识别到阅读任务, 任务章节: {course['title']}")
        return chaoxing.study_read(course, job, job_info)
    # 直播任务
    elif job["type"] == "live":
        logger.trace(f"识别到直播任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        try:
            # 准备直播所需参数
            defaults = {
                "userid": chaoxing.get_uid(),
                "clazzId": course.get("clazzId"),
                "knowledgeid": job_info.get("knowledgeid")
            }
            
            # 创建直播对象
            live = Live(
                attachment=job,
                defaults=defaults,
                course_id=course.get("courseId")
            )
            
            # 启动直播处理线程
            thread = threading.Thread(
                target=LiveProcessor.run_live,
                args=(live, speed),
                daemon=True
            )
            thread.start()
            thread.join()  # 等待直播处理完成
            return StudyResult.SUCCESS
        except Exception as e:
            logger.error(f"处理直播任务时出错: {str(e)}")
            return StudyResult.ERROR

    logger.error(f"未知任务类型: {job['type']}")
    return StudyResult.ERROR


@dataclass(order=True)
class ChapterTask:
    index: int
    point: dict[str, Any] = field(compare=False)
    result: ChapterResult = field(default=ChapterResult.PENDING, compare=False)
    tries: int = field(default=0, compare=False)

class JobProcessor:
    STOP_POINT = {"__stop__": True}

    def __init__(self, chaoxing: Chaoxing, course: dict[str, Any], tasks: list[ChapterTask], config: dict[str, Any]):
        if "jobs" not in config or not config["jobs"]:
            config["jobs"] = 1
        
        self.chaoxing = chaoxing
        self.course = course
        self.speed = config["speed"]
        self.max_tries = 5
        self.tasks = tasks
        self.failed_tasks: list[ChapterTask] = []
        self.task_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.retry_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.wait_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.threads: list[threading.Thread] = []
        self.retry_worker: threading.Thread | None = None
        self.worker_num = config["jobs"]
        self.config = config

    def _stop_task(self) -> ChapterTask:
        return ChapterTask(index=sys.maxsize, point=self.STOP_POINT)

    @staticmethod
    def _is_stop_task(task: ChapterTask) -> bool:
        return task.point.get("__stop__") is True

    def run(self):
        for task in self.tasks:
            self.task_queue.put(task)

        for i in range(self.worker_num):
            thread = threading.Thread(target=self.worker_thread, daemon=True)
            self.threads.append(thread)
            thread.start()

        self.retry_worker = threading.Thread(target=self.retry_thread, daemon=True)
        self.retry_worker.start()

        self.task_queue.join()

        self.retry_queue.put(self._stop_task())
        for _ in self.threads:
            self.task_queue.put(self._stop_task())

        for thread in self.threads:
            thread.join()
        if self.retry_worker:
            self.retry_worker.join()


    @log_error
    def worker_thread(self):
        tqdm.set_lock(tqdm.get_lock())
        while True:
            task = self.task_queue.get()
            if self._is_stop_task(task):
                self.task_queue.task_done()
                logger.debug("Queue worker stopped")
                return

            task.result = process_chapter(self.chaoxing, self.course, task.point, self.speed)

            match task.result:
                case ChapterResult.SUCCESS:
                    logger.debug("Task success: {}", task.point["title"])
                    self.task_queue.task_done()
                    logger.debug(f"unfinished task: {self.task_queue.unfinished_tasks}")

                case ChapterResult.NOT_OPEN:
                    if self.config["notopen_action"] == "continue":
                        logger.warning("章节未开启: {}, 正在跳过", task.point["title"])
                        self.task_queue.task_done()
                        continue

                    task.tries += 1
                    if task.tries >= self.max_tries:
                        logger.error(
                            "章节未开启: {} 可能由于上一章节的章节检测未完成, 也可能由于该章节因为时效已关闭，"
                            "请手动检查完成并提交再重试。或者在配置中配置(自动跳过关闭章节/开启题库并启用提交)"
                        , task.point["title"])
                        self.task_queue.task_done()
                        continue

                    # self.wait_queue.put(task)
                    self.retry_queue.put(task)

                case ChapterResult.ERROR:
                    task.tries += 1
                    logger.warning("Retrying task {} ({}/{} attempts)", task.point["title"], task.tries,
                                   self.max_tries)
                    if task.tries >= self.max_tries:
                        logger.error("Max retries reached for task: {}", task.point["title"])
                        self.failed_tasks.append(task)
                        self.task_queue.task_done()
                        continue
                    self.retry_queue.put(task)

                case _:
                    logger.error("Invalid task state {} for task {}", task.result, task.point["title"])
                    self.failed_tasks.append(task)
                    self.task_queue.task_done()

    @log_error
    def retry_thread(self):
        while True:
            task = self.retry_queue.get()
            if self._is_stop_task(task):
                self.retry_queue.task_done()
                return
            self.task_queue.put(task)
            # task_done is not called when a task failed and needs to be retried so if is reinserted into the queue,
            # the task num will increase by one and become more than the real task number
            self.task_queue.task_done()
            self.retry_queue.task_done()
            time.sleep(1) # TODO: Replace with a configurable wait time


def process_chapter(chaoxing: Chaoxing, course:dict[str, Any], point:dict[str, Any], speed:float) -> ChapterResult:
    """处理单个章节"""
    logger.info(f'当前章节: {point["title"]}')
    if point["has_finished"]:
        logger.info(f'章节：{point["title"]} 已完成所有任务点')
        return ChapterResult.SUCCESS
    
    # 随机等待，避免请求过快
    chaoxing.rate_limiter.limit_rate(random_time=True,random_min=0, random_max=0.2)
    
    # 获取当前章节的所有任务点
    job_info = None
    jobs, job_info = chaoxing.get_job_list(course, point)

    # 发现未开放章节, 根据配置处理
    if job_info.get("notOpen", False):
        return ChapterResult.NOT_OPEN

    # 已经默认处理空任务，此处不需要判断
    if not jobs:
        pass

    # TODO: 个别章节很恶心，多到5个点，可以并行处理，将来会让不同课程不同章节的所有任务点共享一个队列，从而实现全局并行
    # 章节内任务点串行处理: 同一章节只有一个视频/任务在上报进度, 避免被判定多设备并发刷课
    job_results:list[StudyResult]=[]
    with ThreadPoolExecutor(max_workers=1) as executor:
        for result in executor.map(lambda job: process_job(chaoxing, course, job, job_info, speed), jobs):
            job_results.append(result)
    
    for result in job_results:
        if result.is_failure():
            return ChapterResult.ERROR

    return ChapterResult.SUCCESS



def process_course(chaoxing: Chaoxing, course:dict[str, Any], config: dict):
    """处理单个课程"""
    logger.info(f"开始学习课程: {course['title']}")
    
    # 获取当前课程的所有章节
    point_list = chaoxing.get_course_point(
        course["courseId"], course["clazzId"], course["cpi"]
    )
    points = point_list["points"]

    # 识别已经全部看完的课程, 直接跳过, 从尚未看完的课程继续
    unfinished_points = [point for point in points if not point.get("has_finished")]
    if not unfinished_points:
        logger.info(f"课程: {course['title']} 所有章节均已看完, 跳过该课程")
        return

    finished_num = len(points) - len(unfinished_points)
    if finished_num:
        logger.info(
            f"课程: {course['title']} 已识别 {finished_num}/{len(points)} 个章节已看完, "
            f"将从第一个未看完章节: {unfinished_points[0]['title']} 开始"
        )

    # 为了支持课程任务回滚, 采用下标方式遍历任务点

    _old_format_sizeof = tqdm.format_sizeof
    tqdm.format_sizeof = format_time
    tqdm.set_lock(RLock())

    tasks=[]

    for i, point in enumerate(points):
        if point.get("has_finished"):
            logger.info(f'章节：{point["title"]} 已完成所有任务点, 跳过')
            continue
        task = ChapterTask(point=point, index=i)
        tasks.append(task)
    p = JobProcessor(chaoxing, course, tasks, config)
    p.run()


    tqdm.format_sizeof = _old_format_sizeof

def filter_courses(all_course, course_list):
    """过滤要学习的课程"""
    if not course_list:
        # 手动输入要学习的课程ID列表
        print("*" * 10 + "课程列表" + "*" * 10)
        for course in all_course:
            print(f"ID: {course['courseId']} 课程名: {course['title']}")
        print("*" * 28)
        try:
            course_list = input(
                "请输入想要学习的课程列表,以逗号分隔,例: 2151141,189191,198198\n"
            ).split(",")
        except Exception as e:
            raise InputFormatError("输入格式错误") from e
    course_list = [str(course_id).strip() for course_id in course_list if str(course_id).strip()]
    if not course_list:
        logger.warning("未指定课程ID，将学习全部课程")
        return all_course

    # 筛选需要学习的课程
    course_task = []
    course_ids = []
    for course in all_course:
        if course["courseId"] in course_list and course["courseId"] not in course_ids:
            course_task.append(course)
            course_ids.append(course["courseId"])
    
    if not course_task:
        raise InputFormatError(f"未找到指定课程ID: {', '.join(course_list)}")
    
    return course_task


def format_time(num, suffix='', divisor=''):
    total_time = round(num)
    sec = total_time % 60
    mins = (total_time % 3600) // 60
    hrs = total_time // 3600

    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{sec:02d}"

    return f"{mins:02d}:{sec:02d}"


def process_homework(chaoxing: Chaoxing, course: dict[str, Any], homework_config: dict[str, Any]) -> StudyResult:
    """处理单个课程的独立作业答题"""
    logger.info(f"开始处理课程作业: {course['title']}")

    should_submit = homework_config.get("submit", False)
    cover_rate_threshold = homework_config.get("cover_rate", 0.0)
    query_delay = homework_config.get("delay", 0.0)
    export_pdf = homework_config.get("pdf", False)

    try:
        works = get_homework_list(course, status=1)  # 1=未完成
    except HomeworkError as e:
        logger.error(f"获取作业列表失败: {e}")
        return StudyResult.ERROR

    if not works:
        logger.info(f"课程: {course['title']} 无未完成作业, 跳过")
        return StudyResult.SUCCESS

    logger.info(f"课程: {course['title']} 发现 {len(works)} 个未完成作业:")
    for w in works:
        logger.info(f"  - 作业 workId={w.get('workId')} answerId={w.get('answerId')}")

    for work in works:
        try:
            _process_single_homework(chaoxing, course, work, {
                "submit": should_submit,
                "cover_rate": cover_rate_threshold,
                "delay": query_delay,
                "pdf": export_pdf,
            })
        except Exception as e:
            logger.error(f"作业处理失败: {e}")
            logger.error(traceback.format_exc())

    return StudyResult.SUCCESS


def _process_single_homework(chaoxing, course, work, homework_config):
    """处理单个作业: 详情->搜题->保存/提交"""
    # 获取作业详情(题目页)
    form = get_homework_detail(work)

    if not form.get("questions"):
        logger.warning(f"作业 workId={work.get('workId')} 无题目, 跳过")
        return

    # 已存在答案的题目(如之前保存过)保留原答案, 仅补空题
    questions = form.get("questions", [])
    total = len(questions)
    logger.info(f"作业 workId={work.get('workId')} 共 {total} 题")

    found_answers, total_questions = solve_homework(
        chaoxing.tiku,
        form,
        query_delay=homework_config.get("delay", 0.0),
    )

    cover_rate = (found_answers / total_questions * 100) if total_questions else 0.0
    logger.info(f"作业搜题覆盖率: {cover_rate:.0f}%  ({found_answers}/{total_questions})")

    threshold = homework_config.get("cover_rate", 100.0)
    should_submit = homework_config.get("submit", False)

    if should_submit and cover_rate >= threshold:
        _do_submit = True
        _action = "提交"
    elif should_submit:
        _do_submit = False
        _action = "覆盖率未达要求, 仅保存"
    else:
        _do_submit = False
        _action = "配置为仅保存, 不提交"

    logger.info(f"作业 workId={work.get('workId')}: {_action}")
    result = submit_homework(course, work, form, submit=_do_submit)
    if result.is_failure():
        logger.error(f"作业 workId={work.get('workId')} 保存/提交失败")

    if homework_config.get("pdf", False):
        try:
            generate_homework_pdf(course, work, form)
        except Exception as e:
            logger.error(f"作业 PDF 导出失败 workId={work.get('workId')}: {e}")


def main():
    """主程序入口"""
    try:
        # 初始化配置
        common_config, tiku_config, notification_config, homework_config, args = init_config()
        configure_console_logger("DEBUG" if common_config.get("verbose", False) else "INFO")
        
        # 强制播放按照配置文件调节
        common_config["speed"] = min(2.0, max(1.0, common_config.get("speed", 1.0)))
        common_config["notopen_action"] = common_config.get("notopen_action", "retry")
        
        # 初始化超星实例
        config_path = args.config if args.config else None
        chaoxing = init_chaoxing(common_config, tiku_config, config_path)
        
        # 设置外部通知
        notification = Notification()
        notification.config_set(notification_config)
        notification = notification.get_notification_from_config()
        notification.init_notification()
        
        # 检查当前登录状态
        _login_state = chaoxing.login(login_with_cookies=common_config.get("use_cookies", False))
        if not _login_state["status"]:
            raise LoginError(_login_state["msg"])
        
        # 获取所有的课程列表
        all_course = chaoxing.get_course_list()
        
        # 过滤要学习的课程
        course_task = filter_courses(all_course, common_config.get("course_list"))
        
        # 开始学习
        logger.info(f"课程列表过滤完毕, 当前课程任务数量: {len(course_task)}")
        study_enabled = common_config.get("study", True)
        if args.homework_only:
            # 仅作业答题
            for course in course_task:
                process_homework(chaoxing, course, homework_config)
        else:
            # 刷课模块: 学习章节任务点
            if study_enabled:
                for course in course_task:
                    process_course(chaoxing, course, common_config)
            else:
                logger.info("刷课模块已关闭(common.study=0), 跳过章节任务点学习")
            # 按配置运行作业答题(enable=1 时)
            if homework_config.get("enable", False):
                logger.info("开始处理作业答题")
                for course in course_task:
                    process_homework(chaoxing, course, homework_config)
        
        logger.info("所有课程学习任务已完成")
        notification.send("chaoxing : 所有课程学习任务已完成")
        
    except SystemExit as e:
        if e.code != 0:
            logger.error(f"错误: 程序异常退出, 返回码: {e.code}")
        sys.exit(e.code)
    except KeyboardInterrupt as e:
        logger.error(f"错误: 程序被用户手动中断, {e}")
    except BaseException as e:
        logger.error(f"错误: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        try:
            notification.send(f"chaoxing : 出现错误 {type(e).__name__}: {e}\n{traceback.format_exc()}")
        except Exception:
            pass  # 如果通知发送失败，忽略异常
        raise e


if __name__ == "__main__":
    main()
