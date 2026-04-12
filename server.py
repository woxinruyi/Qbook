#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🍅 番茄小说工具箱 v1.4.1 - 后端服务
功能：排行榜抓取 + 智能拆书分析
"""

import os
import sys
import json
import re
import time
import uuid
import socket
import threading
import logging
import webbrowser
import ssl
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.request import urlopen, Request
from urllib.parse import urlparse, parse_qs, quote
from urllib.error import URLError, HTTPError
from pathlib import Path
import jieba
import jieba.analyse

# ==================== 配置 ====================
PORT = 8765
HOST = '127.0.0.1'
# PyInstaller 打包后，资源文件在 sys._MEIPASS；开发时用 __file__ 所在目录
if getattr(sys, 'frozen', False):
    _RESOURCE_DIR = Path(sys._MEIPASS)
    BASE_DIR = Path(sys.executable).parent
else:
    _RESOURCE_DIR = Path(__file__).parent
    BASE_DIR = Path(__file__).parent
CACHE_DIR = BASE_DIR / 'cache'
CACHE_DIR.mkdir(exist_ok=True)
SNAPSHOT_DIR = BASE_DIR / 'snapshots'
SNAPSHOT_DIR.mkdir(exist_ok=True)
DAILY_DIR = BASE_DIR / 'snapshots' / 'daily'
DAILY_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

# ==================== 全局状态 ====================
analysis_tasks = {}      # task_id -> {status, progress, message, result, reasoning}
ranking_cache = {}       # cache_key -> {data, timestamp}
CACHE_DURATION = 86400   # 排行榜缓存24小时（每天拉一次即可）
cache_lock = threading.Lock()   # 保护 ranking_cache 的线程锁
tasks_lock = threading.Lock()   # 保护 analysis_tasks 的线程锁

# 用户 API 配置（从前端同步，持久化到文件）
SETTINGS_FILE = CACHE_DIR / 'api_settings.json'
user_api_config = {}     # {apiKey, baseUrl, model}
user_api_lock = threading.Lock()

def load_user_api_config():
    """从磁盘加载 API 配置"""
    global user_api_config
    try:
        if SETTINGS_FILE.exists():
            raw = SETTINGS_FILE.read_text(encoding='utf-8')
            data = json.loads(raw)
            with user_api_lock:
                user_api_config = data
    except Exception:
        pass

def save_user_api_config(config):
    """保存 API 配置到磁盘"""
    global user_api_config
    with user_api_lock:
        user_api_config = config
    try:
        SETTINGS_FILE.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    except Exception as e:
        logger.warning(f"API 配置保存失败: {e}")

# AI 词云后台分析状态
ai_analysis_status = {}   # cache_key -> {'status': 'running'|'done'|'error', 'message': ''}
ai_analysis_lock = threading.Lock()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# ==================== 日志 ====================
logger = logging.getLogger('toolkit')
logger.setLevel(logging.DEBUG)

def setup_logging():
    """在 main() 中调用，确保日志文件可用"""
    LOG_DIR.mkdir(exist_ok=True)
    _log_handler = logging.FileHandler(LOG_DIR / 'server.log', encoding='utf-8', errors='replace')
    _log_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(_log_handler)


# ==================== 工具函数 ====================
def http_get(url, extra_headers=None, timeout=10):
    """通用 HTTP GET，带 socket 超时兜底防止连接挂死"""
    headers = {**HEADERS, **(extra_headers or {})}
    try:
        # 设置 socket 层面的超时兜底，防止某些情况下 urlopen timeout 不生效
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout + 5)
        try:
            req = Request(url, headers=headers)
            resp = urlopen(req, timeout=timeout, context=SSL_CTX)
            return resp.read().decode('utf-8', errors='replace')
        finally:
            socket.setdefaulttimeout(old_timeout)
    except (socket.timeout, TimeoutError) as e:
        logger.warning(f"HTTP超时 {url}: {e}")
        return None
    except (URLError, HTTPError, OSError, ConnectionError) as e:
        logger.warning(f"HTTP错误 {url}: {e}")
        return None
    except Exception as e:
        logger.error(f"HTTP未知异常 {url}: {type(e).__name__}: {e}")
        return None


def http_get_bytes(url, extra_headers=None, timeout=15):
    """HTTP GET 返回原始字节（用于下载字体等二进制文件）"""
    headers = {**HEADERS, **(extra_headers or {})}
    try:
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout + 5)
        try:
            req = Request(url, headers=headers)
            resp = urlopen(req, timeout=timeout, context=SSL_CTX)
            return resp.read()
        finally:
            socket.setdefaulttimeout(old_timeout)
    except Exception as e:
        logger.warning(f"HTTP下载失败 {url}: {e}")
        return None


def http_post_json(url, data, headers=None, timeout=120):
    """发送 JSON POST 请求"""
    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    hdrs = {
        'Content-Type': 'application/json',
        'User-Agent': HEADERS['User-Agent'],
        **(headers or {})
    }
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout + 5)
    try:
        try:
            req = Request(url, data=body, headers=hdrs)
            resp = urlopen(req, timeout=timeout, context=SSL_CTX)
            return json.loads(resp.read().decode('utf-8'))
        except (socket.timeout, TimeoutError) as e:
            logger.warning(f"POST超时 {url}: {e}")
            raise
        except (URLError, HTTPError, OSError, ConnectionError) as e:
            logger.warning(f"POST错误 {url}: {e}")
            raise
        finally:
            socket.setdefaulttimeout(old_timeout)
    except Exception as e:
        if not isinstance(e, (socket.timeout, TimeoutError, URLError, HTTPError, OSError, ConnectionError)):
            logger.error(f"POST未知异常 {url}: {type(e).__name__}: {e}")
        raise


# ==================== 关键词提取（jieba 分词，无 AI 也能用） ====================
class KeywordExtractor:
    """基于 jieba 的关键词提取器，用于词云基础层"""

    # 网文常见停用词
    STOP_WORDS = {
        '的', '了', '在', '是', '我', '有', '和', '就', '不', '人', '都', '一',
        '上', '也', '很', '到', '说', '要', '去', '你', '会', '着', '看', '好',
        '这', '那', '他', '她', '它', '们', '把', '被', '让', '给', '从', '对',
        '等', '个', '中', '大', '小', '多', '少', '能', '已', '还', '而', '之',
        '为', '以', '及', '或', '但', '却', '又', '地', '得', '过', '下', '来',
        '去', '做', '想', '没', '用', '可以', '一个', '自己', '什么', '怎么',
        '因为', '所以', '如果', '虽然', '但是', '然而', '然后', '之后', '起来',
        '出来', '下来', '进来', '出去', '回来', '起来', '过去', '过来', '知道',
        '时候', '现在', '开始', '已经', '这个', '那个', '这些', '那些', '世界',
        '小说', '故事', '本书', '作者', '作品', '主角', '女主', '男主', '新书',
        '连载', '章节', '完结', '最新', '更新', '点评', '推荐', '积分', '读者',
        '系统', '空间', '时间', '生命', '身体', '灵魂', '力量', '能力', '感觉',
        '事情', '东西', '地方', '样子', '办法', '样子', '眼前', '心中', '手里',
        '不是', '只是', '只有', '就是', '还是', '已经', '正在', '一直', '突然',
        '终于', '渐渐', '慢慢', '轻轻', '默默', '竟然', '居然', '果然', '忽然',
        '到底', '究竟', '也许', '似乎', '仿佛', '好像', '简单', '复杂', '重要',
    }

    # 自定义词典（网文常见词汇，确保不被错误切分）
    CUSTOM_WORDS = [
        '重生', '穿越', '赘婿', '修仙', '修真', '仙侠', '玄幻', '都市',
        '系统流', '种田', '权谋', '争霸', '末世', '赛博朋克', '机甲',
        '灵气', '渡劫', '飞升', '神域', '天道', '法则', '领域', '神通',
        '丹药', '法器', '阵法', '符箓', '妖兽', '魔族', '人族', '龙族',
        '宗门', '世家', '皇朝', '帝国', '王朝', '大陆', '秘境', '遗迹',
        '扮猪吃虎', '退婚', '废物', '天才', '妖孽', '逆袭', '崛起',
        '御兽', '炼丹', '阵法师', '符文师', '剑修', '刀修', '体修',
        '丹田', '经脉', '穴位', '灵根', '筑基', '金丹', '元婴', '化神',
        '大乘', '渡劫', '仙人', '金仙', '太乙', '大罗', '准圣', '圣人',
        '青梅竹马', '白月光', '朱砂痣', '重生文', '穿越文', '快穿',
        '宅斗', '宫斗', '种田文', '甜宠', '虐恋', '先婚后爱', '破镜重圆',
        '娱乐圈', '电竞', '无限流', '末日', '丧尸', '异能', '觉醒',
        '赘婿文', '叶辰', '萧炎', '唐三', '林动', '石昊',
    ]

    _initialized = False

    @classmethod
    def _extract_and_save(cls, cache_key, books):
        """在后台线程中提取关键词并保存到磁盘"""
        try:
            keywords = cls.extract_by_category(books)
            f = CACHE_DIR / f"{cache_key}__kw.json"
            f.write_text(json.dumps(keywords, ensure_ascii=False), encoding='utf-8')
            cats = len([v for v in keywords.values() if v])
            logger.info(f"jieba关键词已保存 {f.name} ({cats}个分类)")
        except Exception as e:
            logger.warning(f"jieba关键词提取失败 {cache_key}: {e}")

    @classmethod
    def _ensure_init(cls):
        """延迟初始化 jieba（首次调用时加载词典，避免启动延迟）"""
        if cls._initialized:
            return
        cls._initialized = True
        # 添加自定义词
        for word in cls.CUSTOM_WORDS:
            jieba.add_word(word, freq=1000)
        # 设置停用词
        for word in cls.STOP_WORDS:
            jieba.analyse.set_stop_words.__func__(jieba.analyse.STOP_WORDS, word) if False else None
        # jieba.analyse 没有 set_stop_words，我们在结果中过滤即可
        # 抑制 jieba 的日志输出
        jieba.setLogLevel(jieba.logging.INFO)

    @classmethod
    def extract(cls, books, top_n=40):
        """
        从书籍列表中提取关键词。
        返回: [{'word': 'xxx', 'heat': 12.5}, ...]
        """
        cls._ensure_init()

        if not books:
            return []

        # 组合所有书名和简介
        texts = []
        for book in books:
            title = book.get('title', '') or ''
            intro = (book.get('intro', '') or '')[:200]
            # 书名权重更高，重复3次
            text = (title + ' ') * 3 + intro
            texts.append(text)

        all_text = ' '.join(texts)

        if len(all_text.strip()) < 10:
            return []

        # 使用 TF-IDF 提取关键词
        try:
            keywords_with_weight = jieba.analyse.extract_tags(
                all_text, topK=top_n * 2, withWeight=True, allowPOS=()
            )
        except Exception:
            return []

        # 过滤并格式化
        result = []
        for word, weight in keywords_with_weight:
            if len(word) < 2:
                continue
            if word in cls.STOP_WORDS:
                continue
            # 过滤纯数字
            if re.match(r'^[\d.]+$', word):
                continue
            result.append({'word': word, 'heat': round(weight * 100, 1)})

        # 去重（jieba 偶尔出重复）
        seen = set()
        unique = []
        for item in result:
            if item['word'] not in seen:
                seen.add(item['word'])
                unique.append(item)

        return unique[:top_n]

    @classmethod
    def extract_by_category(cls, books, top_n=35):
        """
        按分类提取关键词。
        返回: {'all': [...], '玄幻': [...], '都市': [...], ...}
        """
        # 先提取"全部"
        result = {'all': cls.extract(books, top_n)}

        # 按分类分组
        categories = {}
        for book in books:
            cat = book.get('sub_category', '') or ''
            if not cat:
                continue
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(book)

        for cat, cat_books in categories.items():
            if len(cat_books) >= 2:  # 至少2本书才分析
                result[cat] = cls.extract(cat_books, top_n)

        return result


# ==================== 排行榜抓取 ====================
class RankScraper:
    """排行榜数据抓取器"""

    # --- 缓存（内存 + 磁盘持久化，24小时有效） ---
    @staticmethod
    def _cache_file(platform, cache_key):
        return CACHE_DIR / f"{cache_key}.json"

    @staticmethod
    def get_cache(platform, cache_key):
        key = cache_key
        # 先查内存
        with cache_lock:
            if key in ranking_cache:
                entry = ranking_cache[key]
                if time.time() - entry['ts'] < CACHE_DURATION:
                    return entry['data']
        # 再查磁盘
        f = RankScraper._cache_file(platform, cache_key)
        if f.exists():
            try:
                raw = f.read_text(encoding='utf-8')
                data = json.loads(raw)
                # 新格式：直接是结果数据（含books字段）
                if isinstance(data, dict) and 'books' in data:
                    age = time.time() - f.stat().st_mtime
                    if age < CACHE_DURATION:
                        with cache_lock:
                            ranking_cache[key] = {'data': data, 'ts': f.stat().st_mtime}
                        return data
            except Exception as e:
                logger.warning(f"缓存读取失败 {f}: {e}")
        return None

    @staticmethod
    def set_cache(platform, cache_key, data, skip_ai=False):
        key = cache_key
        ts = time.time()
        with cache_lock:
            ranking_cache[key] = {'data': data, 'ts': ts}
        # 持久化到磁盘
        f = RankScraper._cache_file(platform, cache_key)
        try:
            f.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            logger.info(f"缓存已保存 {f.name} ({len(data.get('books', []))}本)")
            # 同时保存每日快照用于趋势对比
            RankScraper.save_snapshot(key, data)
        except Exception as e:
            logger.warning(f"缓存写入失败 {f}: {e}")

        # AI 关键词缓存不触发递归分析
        if platform == 'ai_keywords':
            return

        # 自动提取 jieba 关键词并缓存（异步，不阻塞）
        books = data.get('books', [])
        if len(books) >= 3:
            threading.Thread(target=KeywordExtractor._extract_and_save,
                           args=(cache_key, books), daemon=True).start()

        # 自动触发全分类 AI 词云分析（后台，不阻塞）
        if not skip_ai:
            threading.Thread(target=RankScraper._auto_ai_analysis,
                       args=(cache_key, books), daemon=True).start()

    @staticmethod
    def _auto_ai_analysis(cache_key, books):
        """自动对全分类执行 AI 关键词分析，结果缓存到磁盘"""
        # 检查是否有 API 配置
        with user_api_lock:
            config = dict(user_api_config)
        if not config.get('apiKey'):
            return  # 没有配置 API，跳过

        if len(books) < 3:
            return

        # 提取所有分类
        categories = list(set(b.get('sub_category', '') for b in books if b.get('sub_category')))
        all_targets = [''] + categories  # '' = 全部分类

        for cat in all_targets:
            cat_label = cat or 'all'
            kw_cache_key = f"ai_kw_{cache_key}_{cat_label}"

            # 跳过已有 AI 缓存的
            cached = RankScraper.get_cache('ai_keywords', kw_cache_key)
            if cached and cached.get('keywords'):
                continue

            cat_books = [b for b in books if not cat or b.get('sub_category') == cat] if cat else books
            if len(cat_books) < 3:
                continue

            # 更新状态
            with ai_analysis_lock:
                ai_analysis_status[kw_cache_key] = {'status': 'running', 'message': f'正在分析: {cat or "全部"}'}

            try:
                keywords = RankScraper._call_ai_keywords(cat_books, config)
                ai_result = {
                    'keywords': keywords[:50],
                    'total': len(cat_books),
                    'update_time': time.strftime('%Y-%m-%d %H:%M'),
                }
                RankScraper.set_cache('ai_keywords', kw_cache_key, ai_result, skip_ai=True)
                with ai_analysis_lock:
                    ai_analysis_status[kw_cache_key] = {'status': 'done', 'message': f'完成: {cat or "全部"}'}
                logger.info(f"AI词云分析完成: {cat_label} ({len(keywords)}个词)")
            except Exception as e:
                with ai_analysis_lock:
                    ai_analysis_status[kw_cache_key] = {'status': 'error', 'message': f'{cat or "全部"}: {str(e)[:50]}'}
                logger.warning(f"AI词云分析失败 {cat_label}: {e}")

    @staticmethod
    def _call_ai_keywords(books, api_config):
        """调用 AI API 提取关键词（纯函数，不涉及缓存）"""
        book_lines = []
        for b in books:
            title = b.get('title', '')
            intro = (b.get('intro', '') or '')[:100]
            read_count = b.get('read_count', 0)
            book_lines.append(f"- {title}（在读{read_count}）{intro}")

        books_text = '\n'.join(book_lines[:150])

        prompt = f"""你是网文行业数据分析师。以下是一个小说排行榜的书籍数据（书名、在读人数、简介）：

{books_text}

请分析当前榜单中最高频出现的"爆款元素"——即什么题材、设定、IP、元素最火。

要求：
1. 提取30-50个高频关键词，每个关键词2-6个字
2. 按热度排序（出现频率×人气综合评分）
3. 必须是真正出现在书名或简介中的词，不要自行编造
4. 不要拆分有意义的词（如"三角洲行动"不要拆成"三角""洲""三角洲"）
5. 合并同义词（如"重生"和"重生之"只保留"重生"）

严格按以下JSON格式输出，不要输出任何其他内容：
{{"keywords":[{{"word":"关键词","score":出现次数,"heat":人气加权分}}]}}

示例输出格式：
{{"keywords":[{{"word":"重生","score":12,"heat":45.3}},{{"word":"系统","score":8,"heat":32.1}}]}}"""

        messages = [
            {'role': 'system', 'content': '你是网文数据分析专家，只输出JSON格式结果，不要输出其他内容。'},
            {'role': 'user', 'content': prompt}
        ]

        model = api_config.get('model', 'deepseek-reasoner')
        base_url = api_config.get('baseUrl', 'https://api.deepseek.com').rstrip('/')
        logger.info(f"AI关键词分析(后台): model={model}, books={len(books)}")

        result = BookAnalyzer.call_deepseek(messages, api_config)
        content = result.get('content', '') if isinstance(result, dict) else str(result)

        if not content:
            raise Exception('AI 返回为空')

        # 解析 JSON
        keywords = []
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            try:
                data = json.loads(json_match.group())
                keywords = data.get('keywords', [])
            except json.JSONDecodeError:
                pass

        return keywords

    @staticmethod
    def _kw_cache_file(cache_key):
        return CACHE_DIR / f"{cache_key}__kw.json"

    @staticmethod
    def get_kw_cache(cache_key):
        """获取 jieba 关键词缓存"""
        f = RankScraper._kw_cache_file(cache_key)
        if f.exists():
            try:
                age = time.time() - f.stat().st_mtime
                if age < CACHE_DURATION:
                    return json.loads(f.read_text(encoding='utf-8'))
            except Exception:
                pass
        return None

    @staticmethod
    def _make_result(platform, rank_type, books):
        return {
            'platform': platform,
            'type': rank_type,
            'total': len(books),
            'books': books,
            'update_time': time.strftime('%Y-%m-%d %H:%M')
        }

    # --- 每日快照（用于趋势对比） ---
    @staticmethod
    def _snapshot_file(cache_key, date_str):
        """快照文件路径：snapshots/{date}_{cache_key}.json"""
        safe_key = re.sub(r'[^\w]', '_', cache_key)
        return SNAPSHOT_DIR / f"{date_str}_{safe_key}.json"

    @staticmethod
    def save_snapshot(cache_key, data):
        """保存当前数据为今日快照（记录每本书的 id + 排名 + read_count + word_count，轻量）"""
        today = time.strftime('%Y-%m-%d')
        sf = RankScraper._snapshot_file(cache_key, today)
        if sf.exists():
            return  # 今日已有快照，不覆盖（保留首次抓取数据作为基准）
        try:
            books = data.get('books', [])
            snapshot = {
                'date': today,
                'cache_key': cache_key,
                'book_stats': {
                    b['id']: {
                        'rank': i + 1,
                        'rc': b.get('read_count', 0),
                        'wc': b.get('word_count', 0)
                    }
                    for i, b in enumerate(books) if b.get('id')
                }
            }
            sf.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
            logger.info(f"快照已保存 {sf.name} ({len(snapshot['book_stats'])}本)")
            # 同时保存完整数据备份（用于历史查看）
            RankScraper.save_daily_backup(cache_key, data, today)
        except Exception as e:
            logger.warning(f"快照保存失败: {e}")

    @staticmethod
    def save_daily_backup(cache_key, data, date_str=None):
        """保存完整榜单数据到 snapshots/daily/（用于历史查看）"""
        if not date_str:
            date_str = time.strftime('%Y-%m-%d')
        day_dir = DAILY_DIR / date_str
        day_dir.mkdir(exist_ok=True)
        safe_key = re.sub(r'[^\w]', '_', cache_key)
        f = day_dir / f"{safe_key}.json"
        if f.exists():
            return  # 今日已有备份
        try:
            backup = {
                'date': date_str,
                'cache_key': cache_key,
                'books': data.get('books', []),
                'update_time': data.get('update_time', ''),
                'book_count': len(data.get('books', []))
            }
            f.write_text(json.dumps(backup, ensure_ascii=False), encoding='utf-8')
            logger.info(f"每日备份已保存 {date_str}/{safe_key}.json ({backup['book_count']}本)")
        except Exception as e:
            logger.warning(f"每日备份保存失败: {e}")

    @staticmethod
    def load_yesterday_snapshot(cache_key):
        """加载昨日快照"""
        yesterday = time.strftime('%Y-%m-%d', time.localtime(time.time() - 86400))
        sf = RankScraper._snapshot_file(cache_key, yesterday)
        if not sf.exists():
            return None
        try:
            return json.loads(sf.read_text(encoding='utf-8'))
        except Exception:
            return None

    @staticmethod
    def enrich_with_trends(data, cache_key):
        """给榜单数据中的每本书附加趋势信息（排名变化 + 效率指数 + 阅读量变化）"""
        yesterday = RankScraper.load_yesterday_snapshot(cache_key)
        if not yesterday:
            return data
        y_stats = yesterday.get('book_stats', {})
        for i, b in enumerate(data.get('books', [])):
            bid = b.get('id', '')
            rc = b.get('read_count', 0) or 0
            wc = b.get('word_count', 0) or 0
            rank = i + 1

            # 效率指数 = 在读人数 / 总字数（单位：人/万字）
            wc_wan = wc / 10000
            efficiency = int(rc / wc_wan) if wc_wan > 0 else 0
            b['efficiency'] = efficiency
            b['efficiency_label'] = f"{efficiency}人/万字"

            # 排名变化（核心功能）
            if bid in y_stats:
                old_rank = y_stats[bid].get('rank', 0)
                old_rc = y_stats[bid].get('rc', 0)
                if old_rank > 0:
                    rank_change = old_rank - rank  # 正数=上升(排名数字变小)，负数=下降
                    if rank_change > 0:
                        b['rank_change'] = rank_change
                        b['rank_trend'] = 'up'
                        b['rank_trend_label'] = f'↑{rank_change}'
                    elif rank_change < 0:
                        b['rank_change'] = rank_change
                        b['rank_trend'] = 'down'
                        b['rank_trend_label'] = f'↓{abs(rank_change)}'
                    else:
                        b['rank_change'] = 0
                        b['rank_trend'] = 'stable'
                        b['rank_trend_label'] = '—'
                    # 阅读量趋势（保留旧逻辑用于诊断器）
                    if old_rc > 0:
                        change_pct = round((rc - old_rc) / old_rc * 100)
                        if change_pct >= 15:
                            b['trend'] = 'up'
                            b['trend_label'] = f'+{change_pct}%'
                        elif change_pct <= -15:
                            b['trend'] = 'down'
                            b['trend_label'] = f'{change_pct}%'
                        else:
                            b['trend'] = 'stable'
                            b['trend_label'] = '平稳'
                    else:
                        b['trend'] = 'new'
                        b['trend_label'] = '新上榜'
                else:
                    b['rank_change'] = None
                    b['rank_trend'] = 'unknown'
                    b['rank_trend_label'] = ''
                    b['trend'] = 'unknown'
                    b['trend_label'] = ''
            else:
                # 昨天不在榜单里 → 新晋
                b['rank_change'] = None
                b['rank_trend'] = 'new'
                b['rank_trend_label'] = 'NEW'
                b['trend'] = 'new'
                b['trend_label'] = '新上榜'
        return data

    # --- 番茄小说（动态API） ---

    # ==================== PUA 字体解码器（番茄小说反爬字体） ====================
    _pua_decoder = None
    _pua_decoder_lock = threading.Lock()

    @staticmethod
    def _extract_font_urls(css_text):
        """从 CSS 文本中提取字体文件 URL"""
        urls = []
        for m in re.finditer(r"url\(['\"]?(https?://[^'\")\s]+?\.(?:woff2?|ttf))['\"]?\)", css_text, re.IGNORECASE):
            urls.append(m.group(1))
        return urls

    @staticmethod
    def _build_pua_decoder_pixel(font_url):
        """用像素匹配构建 PUA → Unicode 映射（同源字体精确匹配）"""
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
        from fontTools.ttLib import TTFont

        cache_dir = str(CACHE_DIR)
        decoder_cache = os.path.join(cache_dir, 'pua_decoder.json')
        font_cache = os.path.join(cache_dir, 'fanqie_font.woff2')
        sourcehan_cache = os.path.join(cache_dir, 'SourceHanSansSC-Normal.otf')

        # 1. 如果有缓存的 decoder，直接用
        if os.path.exists(decoder_cache):
            try:
                with open(decoder_cache, 'r', encoding='utf-8') as f:
                    cached = json.load(f)
                if len(cached) > 50:
                    logger.info(f"加载缓存的 PUA 解码器: {len(cached)} 个映射")
                    return {int(k, 16): v for k, v in cached.items()}
            except Exception:
                pass

        # 2. 下载番茄字体
        if not os.path.exists(font_cache):
            font_data = http_get_bytes(font_url, timeout=30)
            if not font_data:
                logger.warning("番茄字体下载失败")
                return {}
            with open(font_cache, 'wb') as f:
                f.write(font_data)
            logger.info(f"番茄字体已缓存: {len(font_data)} bytes")

        # 3. 确保思源黑体存在
        if not os.path.exists(sourcehan_cache):
            sh_url = "https://cdn.jsdelivr.net/gh/adobe-fonts/source-han-sans@release/OTF/SimplifiedChinese/SourceHanSansSC-Normal.otf"
            logger.info("下载思源黑体（用于 PUA 解码）...")
            sh_data = http_get_bytes(sh_url, timeout=120)
            if not sh_data:
                logger.warning("思源黑体下载失败")
                return {}
            with open(sourcehan_cache, 'wb') as f:
                f.write(sh_data)
            logger.info(f"思源黑体已缓存: {len(sh_data)} bytes")

        # 4. 从缓存数据中提取 PUA 字符集
        all_pua = set()
        for fname in os.listdir(cache_dir):
            if fname.startswith('fanqie_') and fname.endswith('.json') and fname != 'pua_decoder.json':
                try:
                    with open(os.path.join(cache_dir, fname), 'r', encoding='utf-8') as f:
                        d = json.load(f)
                    for book in d.get('books', []):
                        for field in ['title', 'intro', 'author', 'sub_category', 'category']:
                            for c in book.get(field, ''):
                                if 0xE000 <= ord(c) <= 0xF8FF:
                                    all_pua.add(ord(c))
                except Exception:
                    pass

        if not all_pua:
            logger.warning("缓存数据中未找到 PUA 字符")
            return {}

        all_pua = sorted(all_pua)
        logger.info(f"发现 {len(all_pua)} 个唯一 PUA 字符，开始构建解码器...")

        # 5. 渲染 PUA 字符
        SIZE = 80
        OUT = 48
        def render_bin(font_path, char_or_cp, size=SIZE, out_size=OUT, threshold=200):
            try:
                font = ImageFont.truetype(font_path, size - 6)
                img = Image.new('L', (size, size), 255)
                draw = ImageDraw.Draw(img)
                text = chr(char_or_cp) if isinstance(char_or_cp, int) else char_or_cp
                bbox = draw.textbbox((0, 0), text, font=font)
                w = bbox[2] - bbox[0]
                h = bbox[3] - bbox[1]
                if w <= 0 or h <= 0:
                    return None
                x = (size - w) // 2 - bbox[0]
                y = (size - h) // 2 - bbox[1]
                draw.text((x, y), text, fill=0, font=font)
                pixels = img.resize((out_size, out_size), Image.LANCZOS)
                return (np.array(pixels) < threshold).astype(np.uint8).flatten()
            except Exception:
                return None

        pua_data = []
        for cp in all_pua:
            arr = render_bin(font_cache, cp)
            if arr is not None and arr.sum() > 3:
                pua_data.append((cp, arr))

        if not pua_data:
            return {}

        # 6. 渲染参考字符（思源黑体 CJK + 标点 + 符号）
        sourcehan = TTFont(sourcehan_cache)
        sh_cmap = sourcehan.getBestCmap()

        ref_arrs = []
        ref_cps_valid = []
        for cp in sh_cmap:
            if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
                0x3000 <= cp <= 0x303F or 0xFF00 <= cp <= 0xFFEF or
                0x2000 <= cp <= 0x206F or 0xF900 <= cp <= 0xFAFF or
                0x0030 <= cp <= 0x0039 or 0x0041 <= cp <= 0x005A or
                0x0061 <= cp <= 0x007A):
                arr = render_bin(sourcehan_cache, cp)
                if arr is not None and arr.sum() > 3:
                    ref_arrs.append(arr)
                    ref_cps_valid.append(cp)

        if not ref_arrs:
            logger.warning("参考字符渲染失败")
            return {}

        ref_matrix = np.array(ref_arrs, dtype=np.uint8)
        total_px = OUT * OUT

        # 7. 匹配
        decoder = {}
        for pua_cp, pua_arr in pua_data:
            diffs = (pua_arr != ref_matrix).sum(axis=1)
            best_idx = int(diffs.argmin())
            best_diff = int(diffs[best_idx])
            if best_diff < total_px * 0.10:  # 10% 阈值
                decoder[pua_cp] = chr(ref_cps_valid[best_idx])

        logger.info(f"PUA 解码器已构建: {len(decoder)} 个映射 (像素匹配, 阈值10%)")

        # 8. 缓存到文件
        if decoder:
            save_data = {hex(k): v for k, v in decoder.items()}
            with open(decoder_cache, 'w', encoding='utf-8') as f:
                json.dump(save_data, f, ensure_ascii=False)

        return decoder

    @staticmethod
    def _build_pua_decoder(font_url):
        """下载字体文件并构建 PUA → Unicode 映射（像素匹配方案）"""
        return RankScraper._build_pua_decoder_pixel(font_url)

    @classmethod
    def _get_pua_decoder(cls):
        """获取 PUA 解码器（懒加载，进程内缓存）"""
        with cls._pua_decoder_lock:
            if cls._pua_decoder is not None:
                return cls._pua_decoder or {}

            # 先尝试直接从缓存文件加载（最快路径）
            cache_dir = str(CACHE_DIR)
            decoder_cache = os.path.join(cache_dir, 'pua_decoder.json')
            if os.path.exists(decoder_cache):
                try:
                    with open(decoder_cache, 'r', encoding='utf-8') as f:
                        cached = json.load(f)
                    if len(cached) > 50:
                        cls._pua_decoder = {int(k, 16): v for k, v in cached.items()}
                        logger.info(f"从缓存加载 PUA 解码器: {len(cls._pua_decoder)} 个映射")
                        return cls._pua_decoder
                except Exception:
                    pass

            meta = cls._fanqie_api_key()
            css = meta.get('font_css', '')
            if not css:
                cls._pua_decoder = {}
                return {}

            urls = cls._extract_font_urls(css)
            if not urls:
                cls._pua_decoder = {}
                return {}

            best = {}
            for url in urls[:1]:  # 只需要1个字体文件
                dec = cls._build_pua_decoder(url)
                if len(dec) > len(best):
                    best = dec

            cls._pua_decoder = best
            return best

    @classmethod
    def decode_pua_text(cls, text):
        """将 PUA 字符替换为正确的 Unicode 字符"""
        decoder = cls._get_pua_decoder()
        if not decoder or not text:
            return text
        result = []
        for ch in text:
            cp = ord(ch)
            if 0xE000 <= cp <= 0xF8FF and cp in decoder:
                result.append(decoder[cp])
            else:
                result.append(ch)
        return ''.join(result)


    @staticmethod
    def _fanqie_api_key():
        """获取番茄API所需的 rank_version、分类列表和字体CSS"""
        cache_key = 'fanqie_meta'
        with cache_lock:
            if cache_key in ranking_cache:
                return ranking_cache[cache_key]['data']

        html = http_get('https://fanqienovel.com/rank')
        if not html:
            return {'rank_version': '', 'categories': {}, 'font_css': ''}

        idx = html.find('__INITIAL_STATE__=')
        if idx < 0:
            return {'rank_version': '', 'categories': {}, 'font_css': ''}

        start = idx + len('__INITIAL_STATE__=')
        end = html.find('</script>', start)
        json_str = html[start:end].strip().rstrip(';').strip()
        json_str = re.sub(r'\bundefined\b', 'null', json_str)

        try:
            state = json.JSONDecoder().raw_decode(json_str)[0]
        except (json.JSONDecodeError, ValueError):
            return {'rank_version': '', 'categories': {}, 'font_css': ''}

        rank = state.get('rank', {})

        # 提取所有字体 CSS @font-face 规则（含 regular/medium/bold 等多个 weight）
        # 去重：只保留非转义版本（过滤掉含 \u002F 的）
        # 关键：注入 unicode-range: U+E000-F8FF，让该字体只渲染 PUA 字符，
        #       避免覆盖正常中文导致字体粗细/风格不一致
        font_css = ''
        ff_matches = re.findall(r'@font-face\{[^}]+\}', html[:80000])
        if ff_matches:
            seen_weights = set()
            unique = []
            for m in ff_matches:
                wm = re.search(r'font-weight:\s*(\d+)', m)
                w = wm.group(1) if wm else '0'
                # 跳过含 Unicode 转义序列的版本（浏览器可能无法解析）
                if '\\u002F' in m or '\\u' in m:
                    continue
                # 限制 unicode-range 到 PUA 私用区，避免影响正常中文字体渲染
                if 'unicode-range' not in m:
                    m = m.rstrip('}') + 'unicode-range:U+E000-F8FF;}'
                if w not in seen_weights:
                    seen_weights.add(w)
                    unique.append(m)
            font_css = '\n'.join(unique)

        meta = {
            'rank_version': rank.get('rankVersion', ''),
            'categories': rank.get('rankCategoryTypeList', {}),
            'font_css': font_css,
        }

        with cache_lock:
            ranking_cache[cache_key] = {'data': meta, 'ts': time.time()}
        return meta

    @staticmethod
    def _fanqie_api_call(category_id, gender='1', rank_mold='2', offset=0, limit=10, retries=2):
        """调用番茄小说排行榜动态API，支持重试"""
        meta = RankScraper._fanqie_api_key()
        url = f"https://fanqienovel.com/api/rank/category/list?app_id=2503&rank_list_type=3&offset={offset}&limit={limit}&category_id={quote(category_id, safe='')}&rank_version={meta['rank_version']}&gender={gender}&rankMold={rank_mold}"

        for attempt in range(1, retries + 1):
            text = http_get(url, extra_headers={
                'Accept': 'application/json, text/plain, */*',
                'Referer': 'https://fanqienovel.com/rank',
            })
            if not text:
                logger.warning(f"API请求失败 cat={category_id} offset={offset} (第{attempt}次)")
                continue
            try:
                data = json.loads(text)
                if data.get('code') != 0:
                    logger.warning(f"API code={data.get('code')} msg={data.get('msg','')[:50]} cat={category_id} offset={offset}")
                return data
            except json.JSONDecodeError as e:
                logger.warning(f"API JSON解析失败 cat={category_id}: {e}, 响应前100字符: {text[:100]}")
                return None
        return None

    @staticmethod
    def _parse_fanqie_books(book_list, rank_offset=0):
        """将API返回的book_list解析为统一格式"""
        books = []
        for i, item in enumerate(book_list):
            if not isinstance(item, dict):
                continue

            book_id = str(item.get('bookId', ''))
            book_name = item.get('bookName', '')
            if not book_name:
                continue

            word_num = item.get('wordNumber', 0)
            # 番茄返回的 wordNumber 可能是字符串或数字，单位是字（不是万字）
            try:
                word_num = int(word_num)
            except (ValueError, TypeError):
                word_num = 0

            # 格式化字数显示
            if word_num > 10000:
                word_display = f"{word_num // 10000}万"
            else:
                word_display = str(word_num)

            # 阅读人数
            read_count = item.get('read_count', 0)
            try:
                read_count = int(read_count)
            except (ValueError, TypeError):
                read_count = 0
            if read_count >= 10000:
                read_display = f"{read_count // 10000}万"
            elif read_count > 0:
                read_display = str(read_count)
            else:
                read_display = ''

            # 封面图
            cover = item.get('thumbUri', '') or ''

            books.append({
                'rank': rank_offset + i + 1,
                'id': book_id,
                'title': book_name,
                'author': item.get('author', ''),
                'intro': (item.get('abstract', '') or '').replace('\\n', '\n')[:200],
                'word_count': word_num,
                'word_display': word_display,
                'read_count': read_count,
                'read_display': read_display,
                'cover': cover,
                'category': item.get('categoryV2', '') or item.get('category', ''),
                'last_chapter': item.get('lastChapterTitle', ''),
                'url': f"https://fanqienovel.com/page/{book_id}",
                'source': '番茄'
            })

        return books

    @staticmethod
    def scrape_fanqie(rank_type='hot', category='', gender=''):
        """
        番茄小说排行榜 - 使用动态API
        rank_type: 'read'(在读榜) / 'new'(新书榜)
        category: 分类名或分类ID，空则按gender取全部分类
        gender: 'male' / 'female' / ''(男频+女频各前5)
        """
        cache_key = f"fanqie_{rank_type}_{gender}_{category}"
        cached = RankScraper.get_cache('fanqie', cache_key)
        if cached:
            return cached

        mold_map = {'read': '2', 'new': '1'}
        rank_mold = mold_map.get(rank_type, '2')

        meta = RankScraper._fanqie_api_key()
        categories = meta['categories']
        rank_version = meta.get('rank_version', '')
        logger.info(f"番茄API meta: rank_version={rank_version[:8] if rank_version else 'EMPTY'}, categories={list(categories.keys())}")

        # 决定要抓取哪些分类
        targets = []  # [(category_id, category_name, gender)]

        # gender参数映射：男频=1, 女频=0（API的gender=2对女频分类无效）
        gender_map = {'male': '1', 'female': '0'}

        if category:
            # 用户指定了分类，查找对应ID
            found = False
            for gender_key in ['male', 'female']:
                for cat in categories.get(gender_key, []):
                    if category in (cat.get('name', ''), cat.get('id', ''), str(cat.get('id', ''))):
                        targets.append((cat['id'], cat['name'], gender_map[gender_key]))
                        found = True
                        break
                if found:
                    break
            if not found:
                # 尝试直接当作 category_id
                targets.append((category, category, '1'))
        elif gender in ('male', 'female'):
            # 指定性别：取该性别全部分类
            g = gender_map[gender]
            for cat in categories.get(gender, []):
                targets.append((cat['id'], cat['name'], g))
        else:
            # 默认：男频前5 + 女频前5
            for gender_key, limit in [('male', 5), ('female', 5)]:
                g = gender_map[gender_key]
                for cat in categories.get(gender_key, [])[:limit]:
                    targets.append((cat['id'], cat['name'], g))

        if not targets:
            # 降级：用已知分类
            targets.append(('261', '都市日常', '1'))

        logger.info(f"番茄爬取: {len(targets)}个分类, gender={gender}, rankType={rank_type}")

        # 对每个分类抓取前30本（3页 x 10本）
        all_books = []
        for cat_id, cat_name, gender in targets:
            cat_book_count = 0
            for offset in range(0, 30, 10):
                resp = RankScraper._fanqie_api_call(
                    category_id=cat_id,
                    gender=gender,
                    rank_mold=rank_mold,
                    offset=offset,
                    limit=10
                )
                if resp and resp.get('code') == 0:
                    bl = resp.get('data', {}).get('book_list', [])
                    parsed = RankScraper._parse_fanqie_books(bl, rank_offset=len(all_books))
                    for b in parsed:
                        b['sub_category'] = cat_name
                    all_books.extend(parsed)
                    cat_book_count += len(parsed)
                    if len(bl) < 10:
                        break  # 没有更多了
                else:
                    if resp:
                        logger.warning(f"API返回code={resp.get('code')} cat={cat_name}({cat_id}) offset={offset}")
                    break
            if cat_book_count == 0:
                logger.warning(f"分类 {cat_name}({cat_id}) 未获取到任何书籍")

        # 如果指定了分类，只返回该分类
        type_label = '在读榜' if rank_type == 'read' else '新书榜'
        gender_label = {'male': '男频', 'female': '女频'}.get(gender, '')
        category_label = category if category else (gender_label if gender else '热门分类')
        result = RankScraper._make_result(f'番茄小说 {category_label} {type_label}', rank_type, all_books)
        RankScraper.set_cache('fanqie', cache_key, result)
        return result

    @staticmethod
    def scrape_fanqie_hot():
        """番茄热门榜（兼容旧接口，从__INITIAL_STATE__获取前10本）"""
        cached = RankScraper.get_cache('fanqie', 'hot')
        if cached:
            return cached

        html = http_get('https://fanqienovel.com/rank')
        if not html:
            return RankScraper._make_result('番茄小说', 'hot', [])

        books = []
        idx = html.find('__INITIAL_STATE__=')
        if idx >= 0:
            start = idx + len('__INITIAL_STATE__=')
            end = html.find('</script>', start)
            json_str = re.sub(r'\bundefined\b', 'null', html[start:end].strip().rstrip(';').strip())
            try:
                state = json.JSONDecoder().raw_decode(json_str)[0]
                bl = state.get('rank', {}).get('book_list', [])
                books = RankScraper._parse_fanqie_books(bl)
            except (json.JSONDecodeError, ValueError):
                pass

        result = RankScraper._make_result('番茄小说', 'hot', books)
        RankScraper.set_cache('fanqie', 'hot', result)
        return result

    @staticmethod
    def get_fanqie_categories():
        """获取番茄小说所有分类"""
        meta = RankScraper._fanqie_api_key()
        cats = []
        for gender_key, gender_label in [('male', '男频'), ('female', '女频')]:
            for cat in meta['categories'].get(gender_key, []):
                cats.append({
                    'id': cat['id'],
                    'name': cat['name'],
                    'gender': gender_label,
                })
        return cats

    @staticmethod
    def get_fanqie_font_css():
        """获取番茄小说的反爬字体 CSS，用于前端渲染 PUA 字符"""
        meta = RankScraper._fanqie_api_key()
        return meta.get('font_css', '')

    # --- 起点中文网 ---
    @staticmethod
    def scrape_qidian(rank_type='hotsales'):
        cache_key = f"qidian_{rank_type}"
        cached = RankScraper.get_cache('qidian', cache_key)
        if cached:
            return cached

        # 优先尝试移动端API
        books = RankScraper._scrape_qidian_mobile(rank_type)

        if not books:
            # 回退到百度搜索
            books = RankScraper._scrape_qidian_search(rank_type)

        result = RankScraper._make_result('起点中文网', rank_type, books)
        if books:
            RankScraper.set_cache('qidian', cache_key, result)
        return result

    @staticmethod
    def _scrape_qidian_mobile(rank_type):
        """尝试通过起点移动端页面获取排行榜"""
        mobile_urls = {
            'hotsales':  'https://m.qidian.com/rank/hotsales/',
            'newbook':   'https://m.qidian.com/rank/newbook/',
            'finished':  'https://m.qidian.com/rank/finished/',
            'recommend': 'https://m.qidian.com/rank/',
            'monthly':   'https://m.qidian.com/rank/yuepiao/',
            'treasure':  'https://m.qidian.com/rank/collect/',
            'fans':      'https://m.qidian.com/rank/fans/',
        }

        url = mobile_urls.get(rank_type, mobile_urls['hotsales'])
        html = http_get(url, extra_headers={
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        })
        if not html:
            return []

        books = []

        # 模式1: 尝试从移动端 __INITIAL_STATE__ 提取（如果有）
        idx = html.find('__INITIAL_STATE__')
        if idx >= 0:
            start = idx + len('__INITIAL_STATE__')
            end = html.find('</script>', start)
            json_str = html[start:end].strip().rstrip(';').strip()
            json_str = re.sub(r'\bundefined\b', 'null', json_str)
            try:
                state = json.JSONDecoder().raw_decode(json_str)[0]
                # 起点移动端数据结构待探索
                for key in ['data', 'rank', 'list']:
                    bl = state.get(key, {})
                    if isinstance(bl, dict):
                        bl = bl.get('bookList', bl.get('list', bl.get('records', [])))
                    if isinstance(bl, list) and bl:
                        for i, item in enumerate(bl):
                            if not isinstance(item, dict):
                                continue
                            bid = str(item.get('bookId', item.get('bId', item.get('id', ''))))
                            title = item.get('bookName', item.get('bName', item.get('name', '')))
                            if not title:
                                continue
                            books.append({
                                'rank': len(books) + 1,
                                'id': bid,
                                'title': title,
                                'author': item.get('authorName', item.get('aName', item.get('author', ''))),
                                'intro': (item.get('bookDesc', item.get('desc', item.get('intro', ''))) or '')[:200],
                                'word_count': item.get('wordCount', item.get('wordNumber', 0)),
                                'category': item.get('categoryName', item.get('catName', item.get('category', ''))),
                                'url': f"https://m.qidian.com/book/{bid}/",
                                'source': '起点'
                            })
                        if books:
                            return books
            except (json.JSONDecodeError, ValueError):
                pass

        # 模式2: 从移动端HTML提取
        # 起点移动端结构：<a href="//m.qidian.com/book/{id}/" ...>
        #   <h2 class="_title_...">书名</h2>
        #   <p class="_bookDesc_...">简介</p>
        #   <p class="_subTitle_...">作者 · 分类 · 字数</p>
        # </a>

        # 先提取每个 <a> 块及其书ID
        blocks = re.findall(
            r'<a\s[^>]*href="//m\.qidian\.com/book/(\d+)/"[^>]*>(.*?)</a>',
            html, re.DOTALL
        )

        for bid, block_html in blocks:
            if len(books) >= 60:
                break

            # 提取书名 (h2 title)
            title_m = re.search(r'<h2[^>]*>(.*?)</h2>', block_html)
            if not title_m:
                title_m = re.search(r'<h[1-6][^>]*>(.*?)</h[1-6]>', block_html)
            if not title_m:
                continue
            title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip()
            if not title or len(title) > 50:
                continue

            # 提取封面 (data-src 优先于 src)
            cover_m = re.search(r'data-src="([^"]*)"', block_html)
            if not cover_m:
                cover_m = re.search(r'<img[^>]+src="([^"]*)"', block_html)
            cover = cover_m.group(1).strip() if cover_m else ''
            # 补全协议
            if cover.startswith('//'):
                cover = 'https:' + cover

            # 提取简介
            desc_m = re.search(r'class="[^"]*bookDesc[^"]*"[^>]*>(.*?)</p>', block_html)
            intro = re.sub(r'<[^>]+>', '', desc_m.group(1)).strip()[:200] if desc_m else ''

            # 提取作者和分类 (subTitle: 作者 · 分类 · 字数)
            sub_m = re.search(r'class="[^"]*subTitle[^"]*"[^>]*>(.*?)</p>', block_html)
            author = ''
            category = ''
            word_count = 0
            if sub_m:
                parts = re.sub(r'<[^>]+>', ' ', sub_m.group(1)).split('·')
                parts = [p.strip() for p in parts if p.strip()]
                if parts:
                    author = parts[0]
                if len(parts) >= 2:
                    category = parts[1]
                if len(parts) >= 3:
                    wc_str = parts[2].replace('万字', '')
                    try:
                        word_count = float(wc_str) * 10000
                    except ValueError:
                        pass

            books.append({
                'rank': len(books) + 1,
                'id': bid,
                'title': title,
                'author': author,
                'intro': intro,
                'word_count': int(word_count),
                'word_display': f"{word_count // 10000:.0f}万" if word_count >= 10000 else str(int(word_count)),
                'cover': cover,
                'category': category,
                'url': f"https://m.qidian.com/book/{bid}/",
                'source': '起点'
            })

        return books

    @staticmethod
    def _scrape_qidian_search(rank_type):
        """通过百度搜索获取起点排行榜数据"""
        # 起点排行榜搜索词
        search_terms = {
            'hotsales':  '起点中文网 畅销榜 2025',
            'newbook':   '起点中文网 新书榜 2025',
            'finished':  '起点中文网 完结榜 2025',
            'recommend': '起点中文网 推荐榜 2025',
            'monthly':   '起点中文网 月票榜 2025',
            'treasure':  '起点中文网 收藏榜 2025',
            'fans':      '起点中文网 人气榜 2025',
        }

        term = search_terms.get(rank_type, search_terms['hotsales'])
        url = f'https://www.baidu.com/s?wd={quote(term)}&rn=30'

        html = http_get(url, extra_headers={
            'Accept-Language': 'zh-CN,zh;q=0.9',
        })

        if not html:
            return []

        books = []
        # 从百度搜索结果中提取书籍信息
        # 匹配书名和链接
        pattern = r'<h3[^>]*class="[^"]*t[^"]*"[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?</h3>'
        matches = re.findall(pattern, html, re.DOTALL)

        seen_titles = set()
        for link, title_html in matches:
            title = re.sub(r'<[^>]+>', '', title_html).strip()
            title = title.replace('起点中文网', '').strip()

            # 过滤掉非书名的结果
            if not title or len(title) < 2 or len(title) > 30:
                continue
            if any(kw in title for kw in ['排行榜', '榜单', '起点', '小说', '推荐', '完本', '畅销', '最新', '热门']):
                continue
            if title in seen_titles:
                continue

            seen_titles.add(title)

            # 尝试从链接中提取书ID
            book_id = ''
            m = re.search(r'book\.qidian\.com/info/(\d+)', link)
            if m:
                book_id = m.group(1)

            books.append({
                'rank': len(books) + 1,
                'id': book_id,
                'title': title,
                'author': '',
                'intro': '',
                'url': link if book_id else f'https://www.qidian.com/search?kw={quote(title)}',
                'source': '起点(搜索)'
            })

            if len(books) >= 30:
                break

        return books

    @staticmethod
    def _scrape_qidian_direct(rank_type):
        """直接抓取起点排行榜页面"""
        url_map = {
            'hotsales':  'https://www.qidian.com/rank/hotsales/',
            'newbook':   'https://www.qidian.com/rank/newbook/',
            'finished':  'https://www.qidian.com/rank/finished/',
            'recommend': 'https://www.qidian.com/rank/',
            'monthly':   'https://www.qidian.com/rank/yuepiao/',
            'treasure':  'https://www.qidian.com/rank/collect/',
        }
        url = url_map.get(rank_type, url_map['hotsales'])
        html = http_get(url)
        if not html:
            return []

        books = []
        # 起点的HTML结构（可能被反爬拦截）
        p1 = re.findall(
            r'<div class="book-mid-info">\s*<h4>\s*<a[^>]*href="//book\.qidian\.com/info/(\d+)/"[^>]*>(.*?)</a>'
            r'.*?</h4>\s*<p class="author">.*?<a[^>]*>(.*?)</a>.*?</p>'
            r'\s*<p class="intro">(.*?)</p>',
            html, re.DOTALL
        )
        for i, (bid, title, author, intro) in enumerate(p1):
            books.append({
                'rank': i + 1, 'id': bid,
                'title': re.sub(r'<[^>]+>', '', title).strip(),
                'author': author.strip(),
                'intro': intro.strip()[:200],
                'url': f'https://book.qidian.com/info/{bid}/',
                'source': '起点'
            })

        if not books:
            p2 = re.findall(r'<a[^>]*href="//book\.qidian\.com/info/(\d+)/"[^>]*>(.*?)</a>', html)
            for i, (bid, title) in enumerate(p2[:50]):
                t = re.sub(r'<[^>]+>', '', title).strip()
                if t and len(t) < 50:
                    books.append({
                        'rank': i + 1, 'id': bid, 'title': t,
                        'author': '未知', 'intro': '',
                        'url': f'https://book.qidian.com/info/{bid}/',
                        'source': '起点'
                    })

        # 去重
        seen = set()
        return [b for b in books if not (b['id'] in seen or seen.add(b['id']))][:60]


# ==================== 智能拆书引擎 ====================
class BookAnalyzer:
    """智能拆书分析 - 自动分块 + 多次调用 + 深度思考模式"""

    # DeepSeek Reasoner 上下文约 64K tokens
    # 中文约 1 token ≈ 1.5 字
    # 安全限制: 每块约 30K 字（预留 prompt + response 空间）
    MAX_CHARS_PER_CHUNK = 28000
    MIN_CHARS_PER_CHUNK = 3000

    @staticmethod
    def split_chapters(text):
        """将小说文本拆分为章节"""
        # 常见章节分隔模式
        patterns = [
            r'^[第零一二三四五六七八九十百千万\d]+\s*章\s*[：:.．:]\s*.+$',
            r'^[Cc]hapter\s+\d+.*$',
            r'^\d+[、.．]\s*.+$',
            r'^卷[零一二三四五六七八九十百千万\d]+\s*.*$',
        ]

        combined = '|'.join(f'({p})' for p in patterns)
        lines = text.split('\n')

        chapters = []
        current_title = '序章'
        current_lines = []

        for line in lines:
            stripped = line.strip()
            if re.match(combined, stripped):
                if current_lines:
                    content = '\n'.join(current_lines).strip()
                    if content:
                        chapters.append({'title': current_title, 'content': content})
                current_title = stripped[:100]
                current_lines = []
            else:
                current_lines.append(line)

        # 最后一个章节
        if current_lines:
            content = '\n'.join(current_lines).strip()
            if content:
                chapters.append({'title': current_title, 'content': content})

        # 如果没检测到章节，按固定字数分割
        if len(chapters) <= 1 and len(text) > 5000:
            chapters = []
            chunk_size = 5000
            for i in range(0, len(text), chunk_size):
                chunk = text[i:i + chunk_size].strip()
                if chunk:
                    chapters.append({
                        'title': f'第{i // chunk_size + 1}部分',
                        'content': chunk
                    })

        return chapters

    @staticmethod
    def create_chunks(chapters):
        """将章节分组为适合 API 调用的块"""
        chunks = []
        current_chunk = []
        current_size = 0

        for ch in chapters:
            ch_size = len(ch['content'])
            # 如果单章就超过限制，截断
            if ch_size > BookAnalyzer.MAX_CHARS_PER_CHUNK:
                # 先保存当前块
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = []
                    current_size = 0
                # 拆分大章节
                for i in range(0, ch_size, BookAnalyzer.MAX_CHARS_PER_CHUNK):
                    part = ch['content'][i:i + BookAnalyzer.MAX_CHARS_PER_CHUNK]
                    chunks.append([{
                        'title': f"{ch['title']} (续{i // BookAnalyzer.MAX_CHARS_PER_CHUNK + 1})",
                        'content': part
                    }])
            elif current_size + ch_size > BookAnalyzer.MAX_CHARS_PER_CHUNK:
                # 当前块已满，开始新块
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = [ch]
                current_size = ch_size
            else:
                current_chunk.append(ch)
                current_size += ch_size

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    @staticmethod
    def build_phase1_prompt(chapters, total_chapters):
        """第一阶段: 结构概览分析"""
        # 只取每章开头和结尾（节省空间）
        book_summary = ''
        for i, ch in enumerate(chapters):
            content = ch['content']
            head = content[:300]
            tail = content[-200:] if len(content) > 500 else ''
            word_count = len(content)
            book_summary += f"\n【{ch['title']}】({word_count}字)\n开头: {head}..."
            if tail:
                book_summary += f"\n结尾: ...{tail}"
            book_summary += '\n'

        return f"""你是资深网文拆书专家，请进行【第一阶段：全书结构概览】分析。

请分析以下小说（共{total_chapters}章，当前为前{len(chapters)}章的摘要）：

{book_summary}

请输出以下内容（Markdown格式）：

## 一、全书核心

### 一句话核心梗
（用20字以内概括全书核心卖点）

### 核心爽点模式
列出2-3个主要爽点模式，每个包含：爽点类型、具体举例、出现频率

### 金手指设定
类型、功能、限制、首次出现章节

### 世界观框架
故事背景、等级/力量体系、核心规则

## 二、核心人设

### 主角身份层次
表面身份、隐藏身份、真正背景

### 主角性格矛盾
核心矛盾点并举例

### 主角动机目标
短期、中期、终极目标

### 重要配角
至少5个，说明功能、关系、特点"""

    @staticmethod
    def build_phase2_prompt(chapters, total_chapters, phase1_result):
        """第二阶段: 深度技法分析"""
        # 选取关键章节：前3章 + 中间抽样 + 最后2章
        selected = []
        if len(chapters) <= 15:
            selected = chapters
        else:
            selected.extend(chapters[:3])  # 前3章
            selected.extend(chapters[-2:])  # 最后2章
            # 中间每隔几章抽一章
            step = max(1, (len(chapters) - 5) // 8)
            for i in range(3, len(chapters) - 2, step):
                selected.append(chapters[i])

        book_text = ''
        for i, ch in enumerate(selected):
            content = ch['content'][:2000]  # 每章最多2000字
            book_text += f"\n【{ch['title']}】\n{content}\n"
            if len(ch['content']) > 2000:
                book_text += '(内容截断)\n'

        return f"""你是资深网文拆书专家，请进行【第二阶段：深度技法分析】。

基于第一阶段的分析结果：
{phase1_result[:4000]}

以下是需要深入分析的关键章节（共{total_chapters}章，精选了{len(selected)}章）：
{book_text}

请输出以下内容（Markdown格式）：

## 三、第一章精析

### 开篇分析
第一句话及其作用

### 冲突引入方式
类型、建立速度、激烈程度

### 主角初印象塑造
前500字内如何建立读者印象

### 期待感钩子
第一章结尾的钩子类型和吸引力

## 四、章节节奏

### 情绪曲线
用文字描述情绪变化趋势

### 爽点间隔
| 章节 | 爽点类型 | 效果评估 |

### 冲突升级链
分析冲突如何逐步升级

### 章末钩子类型
悬念类、冲突类、收获类数量统计

## 五、技法拆解

### 对话占比
分析对话与叙述的比例

### 高频爽词
统计反复出现的"爽词"

### 震惊体/打脸句式模板
归纳典型句式

### 转场技巧
常用转场方式分析

### 装逼打脸公式
标准流程：铺垫→积累→打脸→收尾"""

    @staticmethod
    def build_phase3_prompt(phase1_result, phase2_result, total_chapters):
        """第三阶段: 综合报告"""
        return f"""你是资深网文编辑和网文写作教练。请根据以下两阶段分析结果，生成最终的【完整拆书报告】。

全书共{total_chapters}章。

【第一阶段：结构概览】
{phase1_result}

【第二阶段：技法分析】
{phase2_result}

请输出最终的综合报告（Markdown格式），包含：

## 六、大纲还原

### 三幕式结构
标注全书结构

### 分卷逻辑
推测分卷方式

### 主线支线比例
分析主次线分布

### 伏笔回收表
已埋设的伏笔

## 七、可复用模板

### 开篇模板
从本书提取可复用的开篇模式

### 升级节奏模板
爽点投放节奏模板

### 收尾模板
章末钩子模板

## 八、写作建议

基于分析，给想要模仿此书写法的作者提供5条具体建议。

## 九、评分

| 维度 | 评分(1-10) | 说明 |
|------|-----------|------|
| 开篇吸引力 | | |
| 人设鲜明度 | | |
| 爽点密度 | | |
| 节奏把控 | | |
| 世界观构建 | | |
| 商业潜力 | | |
| 综合推荐 | | |

请确保分析具体、有据可依，多用原文片段佐证。"""

    @staticmethod
    def call_deepseek(messages, api_config):
        """调用 DeepSeek API"""
        base_url = api_config.get('baseUrl', 'https://api.deepseek.com').rstrip('/')
        url = f"{base_url}/v1/chat/completions"
        api_key = api_config.get('apiKey', '')
        model = api_config.get('model', 'deepseek-reasoner')

        if not api_key:
            raise ValueError('请先配置 API Key')

        payload = {
            'model': model,
            'messages': messages,
            'max_tokens': 8000,
            'temperature': 0.7,
        }

        # DeepSeek Reasoner 不支持 temperature
        if model == 'deepseek-reasoner':
            payload.pop('temperature', None)

        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }

        resp = http_post_json(url, payload, headers=headers, timeout=180)

        if 'error' in resp:
            err_msg = resp['error'].get('message', str(resp['error']))
            raise Exception(f"API错误: {err_msg}")

        choice = resp['choices'][0]
        message = choice['message']

        content = message.get('content', '')
        reasoning = message.get('reasoning_content', '')

        return {'content': content, 'reasoning': reasoning}

    @staticmethod
    def run_analysis(task_id, file_content, api_config, modules):
        """执行完整的拆书分析流程"""
        try:
            # 1. 切章
            analysis_tasks[task_id].update({
                'status': 'splitting',
                'progress': 5,
                'message': '正在切章...'
            })

            chapters = BookAnalyzer.split_chapters(file_content)
            total = len(chapters)

            if total == 0:
                raise ValueError('无法识别章节，请检查文件格式')

            analysis_tasks[task_id].update({
                'message': f'共识别 {total} 章，准备分块分析...'
            })

            # 2. 分块
            chunks = BookAnalyzer.create_chunks(chapters)
            analysis_tasks[task_id].update({
                'status': 'analyzing',
                'progress': 10,
                'message': f'已分为 {len(chunks)} 块，开始分析...'
            })

            # 3. 第一阶段：结构概览
            analysis_tasks[task_id].update({
                'message': '第一阶段：全书结构概览...',
                'current_phase': 'phase1'
            })

            # 收集所有章节摘要
            all_chapter_summaries = []
            for ch in chapters:
                all_chapter_summaries.append({
                    'title': ch['title'],
                    'content': ch['content'][:300] + ('...' if len(ch['content']) > 300 else ''),
                    'word_count': len(ch['content'])
                })

            phase1_prompt = BookAnalyzer.build_phase1_prompt(all_chapter_summaries, total)

            phase1_result = BookAnalyzer.call_deepseek(
                [{'role': 'user', 'content': phase1_prompt}],
                api_config
            )

            analysis_tasks[task_id].update({
                'progress': 35,
                'message': '第二阶段：深度技法分析...',
                'phase1': phase1_result['content'],
                'phase1_reasoning': phase1_result.get('reasoning', '')
            })

            # 4. 第二阶段：深度技法
            analysis_tasks[task_id].update({
                'current_phase': 'phase2'
            })

            # 对大书，分块做技法分析
            phase2_parts = []
            for i, chunk in enumerate(chunks):
                analysis_tasks[task_id].update({
                    'progress': 35 + (i / len(chunks)) * 30,
                    'message': f'技法分析中 ({i + 1}/{len(chunks)})...'
                })

                # 只对前3块和最后1块做详细技法分析
                if i < 3 or i == len(chunks) - 1:
                    p2_prompt = BookAnalyzer.build_phase2_prompt(chunk, total, phase1_result['content'])
                    p2_result = BookAnalyzer.call_deepseek(
                        [{'role': 'user', 'content': p2_prompt}],
                        api_config
                    )
                    phase2_parts.append(p2_result['content'])

            phase2_combined = '\n\n---\n\n'.join(phase2_parts)

            analysis_tasks[task_id].update({
                'progress': 65,
                'message': '第三阶段：综合报告生成...',
                'phase2': phase2_combined,
                'current_phase': 'phase3'
            })

            # 5. 第三阶段：综合报告
            phase3_prompt = BookAnalyzer.build_phase3_prompt(
                phase1_result['content'], phase2_combined, total
            )

            phase3_result = BookAnalyzer.call_deepseek(
                [{'role': 'user', 'content': phase3_prompt}],
                api_config
            )

            # 6. 组合最终报告
            final_report = f"""# 拆书分析报告

> 全书共 {total} 章 | 分析模式：深度思考 | 分析时间：{time.strftime('%Y-%m-%d %H:%M')}

---

{phase1_result['content']}

---

{phase2_combined}

---

{phase3_result['content']}"""

            analysis_tasks[task_id].update({
                'status': 'complete',
                'progress': 100,
                'message': '分析完成！',
                'result': final_report,
                'reasoning': phase3_result.get('reasoning', ''),
                'stats': {
                    'total_chapters': total,
                    'chunks': len(chunks),
                    'total_chars': len(file_content),
                }
            })

        except Exception as e:
            analysis_tasks[task_id].update({
                'status': 'error',
                'progress': 0,
                'message': f'分析失败: {str(e)}'
            })
            logger.error(f"任务 {task_id} 失败: {e}", exc_info=True)


# ==================== HTTP 服务 ====================
class ToolkitHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    def log_message(self, format, *args):
        # 静默日志
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            body = content.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404, 'File not found')

    def read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        return self.rfile.read(length)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        try:
            self._handle_GET()
        except ConnectionResetError:
            pass  # 客户端断开，忽略
        except BrokenPipeError:
            pass
        except Exception as e:
            logger.error(f"do_GET 未捕获异常: {type(e).__name__}: {e}", exc_info=True)
            try:
                self.send_error(500, 'Internal Server Error')
            except Exception:
                pass

    def _handle_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/' or path == '':
            self.send_html(_RESOURCE_DIR / 'toolkit.html')

        elif path == '/api/rankings/qidian':
            params = parse_qs(parsed.query)
            rank_type = params.get('type', ['hotsales'])[0]
            result = RankScraper.scrape_qidian(rank_type)
            cache_key = f"qidian_{rank_type}"
            result = RankScraper.enrich_with_trends(result, cache_key)
            self.send_json(result)

        elif path == '/api/rankings/fanqie':
            params = parse_qs(parsed.query)
            rank_type = params.get('type', ['read'])[0]
            category = params.get('category', [''])[0]
            gender = params.get('gender', [''])[0]  # 'male' / 'female' / 空(全部)
            result = RankScraper.scrape_fanqie(rank_type, category, gender)
            # 附带趋势和效率指数
            cache_key = f"fanqie_{rank_type}_{gender}_{category}"
            result = RankScraper.enrich_with_trends(result, cache_key)
            self.send_json(result)

        elif path == '/api/rankings/fanqie/hot':
            result = RankScraper.scrape_fanqie_hot()
            self.send_json(result)

        elif path == '/api/rankings/fanqie/categories':
            cats = RankScraper.get_fanqie_categories()
            self.send_json({'categories': cats})

        elif path == '/api/rankings/fanqie/fontcss':
            css = RankScraper.get_fanqie_font_css()
            if css:
                self.send_response(200)
                self.send_header('Content-Type', 'text/css; charset=utf-8')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'public, max-age=3600')
                body = css.encode('utf-8')
                self.send_header('Content-Length', len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(502, 'Failed to fetch font CSS')

        elif path == '/api/rankings/keywords':
            params = parse_qs(parsed.query)
            cache_key = params.get('cache_key', [''])[0]
            category = params.get('category', [''])[0] or 'all'
            if not cache_key:
                self.send_json({'error': 'cache_key required'}, 400)
                return
            # 优先返回当前分类的 AI 缓存
            ai_kw_key = f"ai_kw_{cache_key}_{category}"
            ai_cached = RankScraper.get_cache('ai_keywords', ai_kw_key)
            if ai_cached and ai_cached.get('keywords'):
                self.send_json({'source': 'ai', 'keywords': ai_cached})
                return
            # 否则返回 jieba 缓存（包含所有分类）
            kw_data = RankScraper.get_kw_cache(cache_key)
            if kw_data:
                self.send_json({'source': 'jieba', 'keywords': kw_data})
            else:
                self.send_json({'source': 'none', 'keywords': {}})

        elif path == '/api/rankings/ai-status':
            """返回所有分类的 AI 词云分析状态"""
            params = parse_qs(parsed.query)
            base_key = params.get('cache_key', [''])[0]
            with ai_analysis_lock:
                status = dict(ai_analysis_status)
            # 如果请求了特定 base_key，只返回匹配的
            if base_key:
                filtered = {k: v for k, v in status.items() if k.startswith(base_key)}
                self.send_json(filtered)
            else:
                self.send_json(status)

        elif path == '/api/rankings/export':
            """导出排行榜数据为 TXT 文件（番茄自动解码 PUA 字符）"""
            params = parse_qs(parsed.query)
            platform = params.get('platform', [''])[0]
            rank_type = params.get('type', [''])[0]
            gender = params.get('gender', [''])[0]

            if not platform or not rank_type:
                self.send_json({'error': '缺少参数 platform 或 type'}, 400)
                return

            # 构建缓存 key
            if platform == 'fanqie':
                cache_key = f"fanqie_{rank_type}_{gender}_"
                plat_label = '番茄小说'
                type_label = '在读榜' if rank_type == 'read' else '新书榜'
                gender_label = {'male': '男频', 'female': '女频', '': ''}.get(gender, '')
            else:
                cache_key = f"qidian_{rank_type}"
                plat_label = '起点中文网'
                type_labels = {'hotsales': '畅销榜', 'newbook': '新书榜', 'monthly': '月票榜'}
                type_label = type_labels.get(rank_type, rank_type)
                gender_label = ''

            data = RankScraper.get_cache(platform, cache_key)
            if not data or not data.get('books'):
                self.send_json({'error': '无数据，请先在扫榜页面加载对应榜单'}, 404)
                return

            # PUA 解码器（仅番茄需要）
            decoder = RankScraper._get_pua_decoder() if platform == 'fanqie' else {}

            def decode(text):
                if not text:
                    return ''
                if not decoder:
                    return text
                return RankScraper.decode_pua_text(text)

            books = data.get('books', [])
            today_str = time.strftime('%Y-%m-%d')
            lines = [
                f"{plat_label} {gender_label}{type_label} ({today_str})",
                f"共 {len(books)} 本",
                "=" * 50,
                "",
            ]

            for i, b in enumerate(books):
                title = decode(b.get('title', ''))
                author = decode(b.get('author', ''))
                intro = decode(b.get('intro', ''))
                category = decode(b.get('sub_category', '') or b.get('category', ''))
                word_count = b.get('word_count', 0) or 0
                read_count = b.get('read_count', 0) or 0

                lines.append(f"【{i+1}】{title}")
                if author:
                    lines.append(f"    作者：{author}")
                if category:
                    lines.append(f"    分类：{category}")
                lines.append(f"    字数：{word_count}字  在读：{read_count}人")
                if intro:
                    lines.append(f"    简介：{intro}")
                lines.append("")

            txt_content = '\n'.join(lines)

            self.send_response(200)
            safe_name = f"{platform}_{rank_type}_{gender}_{today_str}".strip('_')
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Disposition',
                f"attachment; filename*=UTF-8''{quote(f'排行榜_{safe_name}.txt')}")
            body = txt_content.encode('utf-8')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)

        elif path == '/api/settings':
            with user_api_lock:
                self.send_json(user_api_config)

        elif path.startswith('/api/analysis/status/'):
            task_id = path.split('/')[-1]
            if task_id in analysis_tasks:
                task = analysis_tasks[task_id]
                self.send_json({
                    'status': task['status'],
                    'progress': task['progress'],
                    'message': task['message'],
                    'current_phase': task.get('current_phase', ''),
                    'phase1': task.get('phase1', ''),
                    'phase2': task.get('phase2', ''),
                    'result': task.get('result', ''),
                    'reasoning': task.get('reasoning', ''),
                    'stats': task.get('stats', {}),
                })
            else:
                self.send_json({'status': 'not_found', 'message': '任务不存在'}, 404)

        # --- 历史数据 API ---
        elif path == '/api/rankings/history/dates':
            # 获取有历史数据的日期列表
            try:
                dates = sorted([d.name for d in DAILY_DIR.iterdir() if d.is_dir()], reverse=True)
                result = []
                for d in dates:
                    files = [f.stem for f in (DAILY_DIR / d).glob('*.json')]
                    result.append({'date': d, 'rankings': files})
                self.send_json({'dates': result})
            except Exception as e:
                self.send_json({'dates': [], 'error': str(e)})

        elif path.startswith('/api/rankings/history/'):
            # 获取指定日期的榜单数据: /api/rankings/history/2026-04-12/fanqie_new_male
            parts = path[len('/api/rankings/history/'):].split('/')
            if len(parts) >= 2:
                date_str = parts[0]
                cache_key = parts[1]
                f = DAILY_DIR / date_str / f"{cache_key}.json"
                if f.exists():
                    data = json.loads(f.read_text(encoding='utf-8'))
                    data['is_historical'] = True
                    data['historical_date'] = date_str
                    self.send_json(data)
                else:
                    self.send_json({'error': f'未找到 {date_str} 的 {cache_key} 数据'})
            else:
                self.send_json({'error': '格式：/api/rankings/history/日期/榜单key'})

        else:
            self.send_error(404)

    def do_POST(self):
        try:
            self._handle_POST()
        except ConnectionResetError:
            pass
        except BrokenPipeError:
            pass
        except Exception as e:
            logger.error(f"do_POST 未捕获异常: {type(e).__name__}: {e}", exc_info=True)
            try:
                self.send_json({'error': '服务器内部错误'}, 500)
            except Exception:
                pass

    def _handle_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/api/analyze':
            try:
                body = json.loads(self.read_body())
                file_content = body.get('content', '')
                api_config = body.get('apiConfig', {})
                modules = body.get('modules', {})

                if not file_content:
                    self.send_json({'error': '请上传小说文件'}, 400)
                    return
                if not api_config.get('apiKey'):
                    self.send_json({'error': '请配置 API Key'}, 400)
                    return

                task_id = str(uuid.uuid4())[:8]
                analysis_tasks[task_id] = {
                    'status': 'queued',
                    'progress': 0,
                    'message': '排队中...',
                }

                # 后台线程执行
                t = threading.Thread(
                    target=BookAnalyzer.run_analysis,
                    args=(task_id, file_content, api_config, modules),
                    daemon=True
                )
                t.start()

                self.send_json({'task_id': task_id, 'message': '任务已创建'})

            except json.JSONDecodeError:
                self.send_json({'error': '无效的请求数据'}, 400)
            except Exception as e:
                self.send_json({'error': str(e)}, 500)

        if path == '/api/settings':
            """保存用户 API 配置到后端"""
            try:
                body = json.loads(self.read_body())
                api_key = body.get('apiKey', '').strip()
                base_url = body.get('baseUrl', '').strip()
                model = body.get('model', '').strip()
                config = {'apiKey': api_key, 'baseUrl': base_url, 'model': model}
                save_user_api_config(config)
                self.send_json({'message': 'ok'})
            except Exception as e:
                self.send_json({'error': str(e)}, 500)

        elif path == '/api/ai/keywords':
            """AI 智能提取榜单高频爆款关键词"""
            try:
                body = json.loads(self.read_body())
                books = body.get('books', [])
                api_config = body.get('apiConfig', {})
                cache_key = body.get('cacheKey', '')

                if not books:
                    self.send_json({'error': '无书籍数据'}, 400)
                    return
                if not api_config.get('apiKey'):
                    self.send_json({'error': 'no_api_key'}, 400)
                    return

                # 检查磁盘缓存
                if cache_key:
                    cached = RankScraper.get_cache('ai_keywords', cache_key)
                    if cached:
                        self.send_json(cached)
                        return

                # 构造提示词
                book_lines = []
                for b in books:
                    title = b.get('title', '')
                    intro = (b.get('intro', '') or '')[:100]
                    read_count = b.get('read_count', 0)
                    book_lines.append(f"- {title}（在读{read_count}）{intro}")

                books_text = '\n'.join(book_lines[:150])

                prompt = f"""你是网文行业数据分析师。以下是一个小说排行榜的书籍数据（书名、在读人数、简介）：

{books_text}

请分析当前榜单中最高频出现的"爆款元素"——即什么题材、设定、IP、元素最火。

要求：
1. 提取30-50个高频关键词，每个关键词2-6个字
2. 按热度排序（出现频率×人气综合评分）
3. 必须是真正出现在书名或简介中的词，不要自行编造
4. 不要拆分有意义的词（如"三角洲行动"不要拆成"三角""洲""三角洲"）
5. 合并同义词（如"重生"和"重生之"只保留"重生"）

严格按以下JSON格式输出，不要输出任何其他内容：
{{"keywords":[{{"word":"关键词","score":出现次数,"heat":人气加权分}}]}}

示例输出格式：
{{"keywords":[{{"word":"重生","score":12,"heat":45.3}},{{"word":"系统","score":8,"heat":32.1}}]}}"""

                messages = [
                    {'role': 'system', 'content': '你是网文数据分析专家，只输出JSON格式结果，不要输出其他内容。'},
                    {'role': 'user', 'content': prompt}
                ]

                model = api_config.get('model', 'deepseek-reasoner')
                base_url = api_config.get('baseUrl', 'https://api.deepseek.com').rstrip('/')
                logger.info(f"AI关键词分析: model={model}, baseUrl={base_url}, books={len(books)}, cacheKey={cache_key}")

                result = BookAnalyzer.call_deepseek(messages, api_config)

                # call_deepseek 返回 {'content': ..., 'reasoning': ...}
                content = result.get('content', '') if isinstance(result, dict) else str(result)

                if not content:
                    raise Exception('AI 返回为空，请检查 API Key 和模型名称是否正确')

                # 解析 JSON（容错：提取 JSON 块）
                keywords = []
                import re as _re
                json_match = _re.search(r'\{[\s\S]*\}', content)
                if json_match:
                    try:
                        data = json.loads(json_match.group())
                        keywords = data.get('keywords', [])
                    except json.JSONDecodeError:
                        pass

                ai_result = {
                    'keywords': keywords[:50],
                    'total': len(books),
                    'update_time': time.strftime('%Y-%m-%d %H:%M'),
                }

                # 缓存结果（24小时）
                if cache_key:
                    RankScraper.set_cache('ai_keywords', cache_key, ai_result)

                self.send_json(ai_result)

            except Exception as e:
                logger.error(f"AI关键词提取失败: {e}")
                try:
                    self.send_json({'error': str(e)}, 500)
                except Exception:
                    pass  # 客户端已断开，忽略

        elif path == '/api/rankings/refresh':
            # 清除缓存并重新抓取
            raw = self.read_body()
            body = json.loads(raw) if raw else {}
            platform = body.get('platform', 'all')

            if platform in ('qidian', 'all'):
                for key in list(ranking_cache.keys()):
                    if key.startswith('qidian_'):
                        del ranking_cache[key]
                for f in CACHE_DIR.glob('qidian_*.json'):
                    f.unlink()
            if platform in ('fanqie', 'all'):
                for key in list(ranking_cache.keys()):
                    if key.startswith('fanqie_'):
                        del ranking_cache[key]
                for f in CACHE_DIR.glob('fanqie_*.json'):
                    f.unlink()

            self.send_json({'message': '缓存已清除'})

        else:
            self.send_error(404)


# ==================== 启动 ====================

# 记录上次自动拉取的日期，防止重复执行
_last_auto_fetch_date = None
_auto_fetch_lock = threading.Lock()

def _daily_auto_fetch():
    """每天 7:00 自动拉取默认榜单数据（后台线程循环）"""
    global _last_auto_fetch_date
    while True:
        now = datetime.now()
        # 计算距离下一个 7:00 的秒数
        target = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_secs = (target - now).total_seconds()
        logger.info(f"定时任务：下次自动拉取将在 {target.strftime('%Y-%m-%d %H:%M')}，等待 {wait_secs:.0f} 秒")

        time.sleep(wait_secs)

        with _auto_fetch_lock:
            today = datetime.now().strftime('%Y-%m-%d')
            if _last_auto_fetch_date == today:
                continue
            _last_auto_fetch_date = today

        logger.info(f"定时任务：开始每日自动拉取榜单数据 ({today})")
        try:
            # 清除旧缓存
            with cache_lock:
                for key in list(ranking_cache.keys()):
                    if key.startswith('fanqie_'):
                        del ranking_cache[key]
            for f in CACHE_DIR.glob('fanqie_*.json'):
                if 'pua_decoder' not in f.name:
                    f.unlink()

            # 拉取男频新书榜（默认页面）
            RankScraper.scrape_fanqie(rank_type='new', gender='male')
            # 拉取男频在读榜
            RankScraper.scrape_fanqie(rank_type='read', gender='male')
            # 拉取女频新书榜
            RankScraper.scrape_fanqie(rank_type='new', gender='female')
            # 拉取女频在读榜
            RankScraper.scrape_fanqie(rank_type='read', gender='female')

            logger.info(f"定时任务：每日自动拉取完成 ({today})")
        except Exception as e:
            logger.error(f"定时任务：自动拉取失败 - {e}")


def main():
    setup_logging()
    load_user_api_config()
    logger.info("服务器启动中...")

    print(f"""
============================================
     番茄小说工具箱 v1.4.0
     排行榜抓取 + 智能拆书分析
============================================
     服务地址: http://{HOST}:{PORT}
     日志文件: {LOG_DIR / 'server.log'}
     定时拉取: 每天 07:00 自动更新榜单
     按 Ctrl+C 停止服务
============================================
""")

    # 启动每日定时拉取线程
    fetch_thread = threading.Thread(target=_daily_auto_fetch, daemon=True, name='daily-fetch')
    fetch_thread.start()

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadingHTTPServer((HOST, PORT), ToolkitHandler)

    # 自动打开浏览器
    threading.Timer(1.0, lambda: webbrowser.open(f'http://{HOST}:{PORT}')).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务已停止")
        server.server_close()


if __name__ == '__main__':
    main()
