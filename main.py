print("程序开始执行...")  # 添加在文件最开始

from flask import Flask, render_template, request, send_file, jsonify, abort, url_for
import os
import tempfile
import fitz  # PyMuPDF
from docx import Document
import mimetypes
import hashlib
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import Paragraph, Frame, PageBreak, Spacer, Table, TableStyle, Flowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, cm
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER
import requests
import json
import logging
from io import BytesIO
import time
import re
import uuid
import threading
from datetime import datetime
from pathlib import Path
import socket

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()  # 添加控制台输出
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

UPLOAD_FOLDER = 'uploads'
PROCESSED_FOLDER = 'processed'  # 存储处理完成的文件
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PROCESSED_FOLDER, exist_ok=True)

# 文件上传限制
MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10MB
ALLOWED_EXTENSIONS = {'pdf', 'docx'}
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

# 任务状态存储
tasks = {}

# DeepSeek API设置
DEEPSEEK_API_KEY = "sk-3023005df59f47e794935db462219c93"
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
API_TIMEOUT = 60  # API请求超时时间（秒）
MAX_RETRIES = 3   # API请求最大重试次数

def check_dependencies():
    missing_deps = []
    try:
        import flask
    except ImportError:
        missing_deps.append("flask")
    try:
        import docx
    except ImportError:
        missing_deps.append("python-docx")
    try:
        import fitz
    except ImportError:
        missing_deps.append("PyMuPDF")
    try:
        import reportlab
    except ImportError:
        missing_deps.append("reportlab")
    try:
        import requests
    except ImportError:
        missing_deps.append("requests")
    
    if missing_deps:
        print("缺少以下依赖包，请使用pip安装：")
        for dep in missing_deps:
            print(f"pip install {dep}")
        exit(1)

def allowed_file(filename):
    """检查文件名是否有允许的扩展名"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def verify_file_type(file_path):
    """验证文件类型的真实性"""
    # 使用文件扩展名判断
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.pdf':
        return 'pdf'
    elif ext == '.docx':
        return 'docx'
    return None

def extract_text_from_pdf(file_path):
    """从PDF文件中提取文本"""
    try:
        doc = fitz.open(file_path)
        text = ""
        for page in doc:
            text += page.get_text()
        return text
    except Exception as e:
        logger.error(f"PDF文本提取错误: {str(e)}")
        raise Exception(f"无法从PDF文件提取文本: {str(e)}")

def extract_text_from_docx(file_path):
    """从Word文件中提取文本"""
    try:
        doc = Document(file_path)
        text = ""
        for para in doc.paragraphs:
            text += para.text + "\n"
        return text
    except Exception as e:
        logger.error(f"DOCX文本提取错误: {str(e)}")
        raise Exception(f"无法从Word文件提取文本: {str(e)}")

def call_api_with_retry(endpoint, headers, data):
    """使用重试逻辑调用API"""
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(
                endpoint, 
                headers=headers, 
                json=data,
                timeout=API_TIMEOUT
            )
            response.raise_for_status()  # 如果响应状态是4xx或5xx则抛出异常
            return response.json()
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            logger.warning(f"API调用失败 (尝试 {attempt+1}/{MAX_RETRIES}): {str(e)}")
            if attempt == MAX_RETRIES - 1:  # 最后一次尝试
                logger.error(f"API调用失败，已达到最大重试次数: {str(e)}")
                raise Exception(f"与AI服务通信失败: {str(e)}")
            time.sleep(2 ** attempt)  # 指数退避策略

def identify_difficult_words(text, level):
    """使用DeepSeek API识别难词"""
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # 限制文本长度，避免API限制
    max_text_length = 4000  # 减小文本长度限制
    truncated_text = text[:max_text_length] if len(text) > max_text_length else text
    
    prompt = f"""
    请识别以下英文文本中对{level}水平的英语学习者来说较难的单词。
    请严格按照以下JSON格式返回结果：
    [
        {{"word": "难词1", "definition": "中文释义 (English definition)"}},
        {{"word": "难词2", "definition": "中文释义 (English definition)"}}
    ]
    
    文本：
    {truncated_text}
    """
    
    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 2000
    }
    
    try:
        result = call_api_with_retry(DEEPSEEK_API_URL, headers, data)
        
        if not result or "choices" not in result:
            logger.error(f"API返回无效响应: {result}")
            return []
            
        content = result["choices"][0]["message"]["content"]
        
        # 查找JSON字符串
        json_match = re.search(r'\[\s*\{.*\}\s*\]', content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                logger.error("JSON解析失败")
                return []
        else:
            logger.error("未找到有效的JSON格式")
            return []
            
    except Exception as e:
        logger.error(f"处理API响应时出错: {str(e)}")
        return []

def generate_introduction(text, level):
    """使用DeepSeek API生成导读"""
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # 限制文本长度
    max_text_length = 4000
    truncated_text = text[:max_text_length] if len(text) > max_text_length else text
    
    prompt = f"""
    请为以下英文文章创建一段简短的中文导读，概述文章的主要内容和目的。
    这是针对{level}水平的英语学习者，请使用简单明了的中文。
    请确保返回的是UTF-8编码的纯文本，不要包含任何特殊字符或格式标记。
    
    文本：
    {truncated_text}
    """
    
    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 800
    }
    
    try:
        result = call_api_with_retry(DEEPSEEK_API_URL, headers, data)
        if not result or "choices" not in result:
            return "无法生成导读"
        return result["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"生成导读时出错: {str(e)}")
        return "生成导读时发生错误"

def generate_summary_and_structure(text, level):
    """使用DeepSeek API生成摘要和结构图"""
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # 限制文本长度
    max_text_length = 4000
    truncated_text = text[:max_text_length] if len(text) > max_text_length else text
    
    prompt = f"""
    请为以下英文文章创建（使用简洁明了的中文）：
    1. 一份简短的摘要，概述文章主要内容
    2. 一份文章结构图，以简单的列表格式展示文章的主要部分
    
    这是针对{level}水平的英语学习者，请确保返回的是UTF-8编码的纯文本，不要包含任何特殊字符或格式标记。
    
    文本：
    {truncated_text}
    """
    
    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 1000
    }
    
    try:
        result = call_api_with_retry(DEEPSEEK_API_URL, headers, data)
        if not result or "choices" not in result:
            return "无法生成摘要和结构图"
        return result["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"生成摘要和结构图时出错: {str(e)}")
        return "生成摘要和结构图时发生错误"

# 自定义Flowable类，用于在PDF中添加行内标注
class AnnotatedText(Flowable):
    def __init__(self, text, words_to_highlight, word_definitions, style):
        Flowable.__init__(self)
        self.text = text
        self.words = words_to_highlight
        self.definitions = word_definitions
        self.style = style
        self.width = 0
        self.height = 0
        
    def draw(self):
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.colors import blue
        from reportlab.pdfbase.pdfmetrics import stringWidth
        
        # 设置基本属性
        canvas = self.canv
        style = self.style
        font_name = style.fontName
        font_size = style.fontSize
        
        # 设置初始位置
        x, y = 0, 0
        
        # 将文本分割成单词
        words = re.findall(r'\b\w+\b|\S+|\s+', self.text)
        
        # 计算行高
        line_height = font_size * 1.2
        
        # 记录当前行的宽度和当前位置
        current_line_width = 0
        max_width = 450  # 最大行宽
        
        # 绘制文本，突出显示难词
        for word in words:
            # 计算当前单词的宽度
            word_width = stringWidth(word, font_name, font_size)
            
            # 检查是否需要换行
            if current_line_width + word_width > max_width:
                y -= line_height
                current_line_width = 0
                x = 0
            
            # 检查是否是需要突出显示的单词
            highlight = False
            definition = ""
            for w in self.words:
                if word.lower() == w.lower() or word.lower() == w.lower()+".":
                    highlight = True
                    definition = self.definitions.get(w, "")
                    break
            
            # 绘制单词
            if highlight:
                canvas.setFillColor(blue)
                canvas.drawString(x, y, word)
                
                # 在右侧添加标注（如果有空间）
                if definition and current_line_width + word_width + 10 < max_width:
                    note_x = x + word_width + 5
                    canvas.setFillColor(colors.red)
                    canvas.setFont(font_name, font_size * 0.8)
                    # 截断过长的定义
                    short_def = definition[:30] + "..." if len(definition) > 30 else definition
                    canvas.drawString(note_x, y, f"({short_def})")
                    # 恢复原始字体大小
                    canvas.setFont(font_name, font_size)
                
                canvas.setFillColor(colors.black)
            else:
                canvas.setFillColor(colors.black)
                canvas.drawString(x, y, word)
            
            # 更新位置
            x += word_width
            current_line_width += word_width
        
        # 更新流对象的高度
        self.height = abs(y) + line_height
        
    def wrap(self, availWidth, availHeight):
        # 估算大小
        return (availWidth, self.height)

def create_annotated_pdf(text, difficult_words, introduction, summary_structure, orig_filename):
    """创建带有标注的PDF文档，具有两栏布局和中文支持"""
    buffer = BytesIO()
    
    # 使用ReportLab的文档构建功能
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    
    # 注册中文字体
    try:
        # 尝试注册思源黑体 (可根据您系统上的字体修改)
        pdfmetrics.registerFont(TTFont('SimSun', 'SimSun.ttf'))
        chinese_font = 'SimSun'
    except:
        try:
            # 尝试注册系统上可能存在的其他中文字体
            pdfmetrics.registerFont(TTFont('Microsoft YaHei', 'msyh.ttf'))
            chinese_font = 'Microsoft YaHei'
        except:
            # 如果都失败，可以尝试使用内嵌中文字体
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
            pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
            chinese_font = 'STSong-Light'
            logger.warning("使用内置中文字体 STSong-Light")
    
    # 设置页面大小和边距
    page_width, page_height = A4
    doc = SimpleDocTemplate(
        buffer, 
        pagesize=A4,
        leftMargin=1.5*cm,
        rightMargin=1.5*cm,
        topMargin=2*cm,
        bottomMargin=2*cm,
        title=f"{orig_filename}_标注"
    )
    
    # 创建自定义样式，全部使用中文字体
    styles = getSampleStyleSheet()
    
    # 标题样式
    title_style = ParagraphStyle(
        'ChineseTitle',
        parent=styles['Title'],
        fontName=chinese_font,
        fontSize=16,
        spaceAfter=12,
        alignment=1  # 居中
    )
    
    # 标题样式
    heading_style = ParagraphStyle(
        'ChineseHeading',
        parent=styles['Heading1'],
        fontName=chinese_font,
        fontSize=14,
        spaceAfter=10,
        spaceBefore=10
    )
    
    # 正文样式
    normal_style = ParagraphStyle(
        'ChineseBody',
        parent=styles['Normal'],
        fontName=chinese_font,
        fontSize=10,
        leading=14,
        spaceAfter=6
    )
    
    # 难词样式（蓝色）
    difficult_word_style = ParagraphStyle(
        'DifficultWord',
        parent=normal_style,
        textColor=colors.blue,
        fontName=chinese_font
    )
    
    # 注释样式（右栏）
    note_style = ParagraphStyle(
        'Note',
        parent=normal_style,
        fontSize=9,
        fontName=chinese_font,
        leading=11
    )
    
    # 词汇表单词样式
    glossary_word_style = ParagraphStyle(
        'GlossaryWord',
        parent=normal_style,
        textColor=colors.blue,
        fontName=chinese_font,
        fontSize=10
    )
    
    # 词汇表定义样式
    glossary_def_style = ParagraphStyle(
        'GlossaryDef',
        parent=normal_style,
        fontName=chinese_font,
        fontSize=10
    )
    
    # 准备内容
    story = []
    
    # 标题
    story.append(Paragraph(f"{orig_filename} - AI标注文档", title_style))
    story.append(Spacer(1, 0.5*cm))
    
    # 导读部分
    story.append(Paragraph("导读", heading_style))
    story.append(Paragraph(introduction, normal_style))
    story.append(Spacer(1, 0.5*cm))
    
    # 创建两栏表格
    # 左栏宽度 - 文本宽度的65%
    left_width = (page_width - 3*cm) * 0.65
    # 右栏宽度 - 文本宽度的35%
    right_width = (page_width - 3*cm) * 0.35
    
    # 正文标题
    story.append(Paragraph("正文与难词注释", heading_style))
    
    # 创建一个字典，保存每个难词及其定义
    word_dict = {word_info["word"]: word_info["definition"] for word_info in difficult_words}
    
    # 将文本分割成段落
    paragraphs = text.split('\n')
    
    # 遍历段落，创建两栏内容
    for para_idx, para in enumerate(paragraphs):
        if not para.strip():
            continue
            
        # 处理段落，标记难词
        marked_para = para
        notes = []  # 此段落中的注释
        
        # 按长度排序单词，先处理长的避免部分匹配问题
        for word in sorted(word_dict.keys(), key=len, reverse=True):
            # 使用正则匹配整词
            pattern = r'\b' + re.escape(word) + r'\b'
            # 如果找到匹配
            if re.search(pattern, marked_para, re.IGNORECASE):
                # 添加到注释列表
                notes.append((word, word_dict[word]))
                # 替换为带下划线的蓝色文字
                marked_para = re.sub(
                    pattern, 
                    f'<font color="blue"><u>{word}</u></font>', 
                    marked_para, 
                    flags=re.IGNORECASE
                )
        
        # 创建左栏内容 - 带标记的段落
        left_cell = Paragraph(marked_para, normal_style)
        
        # 创建右栏内容 - 注释
        right_content = ""
        for word, definition in notes:
            right_content += f"<b>{word}</b>: {definition}<br/>"
        
        # 如果没有注释，添加空白
        right_cell = Paragraph(right_content, note_style) if right_content else ""
        
        # 创建表格行
        table_data = [[left_cell, right_cell]]
        
        # 创建表格并设置样式
        table = Table(
            table_data, 
            colWidths=[left_width, right_width],
            style=[
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('LEFTPADDING', (0, 0), (0, 0), 0),  # 左栏无左内边距
                ('RIGHTPADDING', (0, 0), (0, 0), 6),  # 左栏右内边距
                ('LEFTPADDING', (1, 0), (1, 0), 6),  # 右栏左内边距
                ('RIGHTPADDING', (1, 0), (1, 0), 0),  # 右栏无右内边距
            ]
        )
        
        story.append(table)
        story.append(Spacer(1, 0.2*cm))
    
    # 添加分页
    story.append(PageBreak())
    
    # 词汇表
    if difficult_words:
        story.append(Paragraph("词汇表", heading_style))
        
        # 创建表格数据
        glossary_data = []
        for word_info in difficult_words:
            glossary_data.append([
                Paragraph(word_info["word"], glossary_word_style),
                Paragraph(word_info["definition"], glossary_def_style)
            ])
        
        # 创建表格
        if glossary_data:
            glossary_table = Table(
                glossary_data, 
                colWidths=[4*cm, 12*cm],
                style=[
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('GRID', (0, 0), (-1, -1), 0.25, colors.grey),
                    ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey)
                ]
            )
            story.append(glossary_table)
    
    # 添加分页
    story.append(PageBreak())
    
    # 摘要和结构图
    story.append(Paragraph("摘要和结构图", heading_style))
    story.append(Paragraph(summary_structure, normal_style))
    
    # 构建PDF
    doc.build(story)
    
    buffer.seek(0)
    return buffer

def process_document_task(task_id, file_path, level, filename):
    """异步处理文档的任务"""
    try:
        # 更新任务状态
        tasks[task_id]['status'] = 'processing'
        tasks[task_id]['progress'] = 10
        
        # 验证文件类型
        file_type = verify_file_type(file_path)
        if not file_type:
            tasks[task_id]['status'] = 'failed'
            tasks[task_id]['error'] = "文件类型不匹配，请上传有效的PDF或DOCX文件。"
            return
        
        tasks[task_id]['progress'] = 20
        
        # 根据文件类型提取文本
        try:
            if file_type == 'pdf':
                text = extract_text_from_pdf(file_path)
            elif file_type == 'docx':
                text = extract_text_from_docx(file_path)
                
            logger.info(f"成功提取文本，长度: {len(text)} 字符")
        except Exception as e:
            tasks[task_id]['status'] = 'failed'
            tasks[task_id]['error'] = f"无法从文件中提取文本: {str(e)}"
            return
        
        # 处理过短的文本
        if len(text.strip()) < 50:
            tasks[task_id]['status'] = 'failed'
            tasks[task_id]['error'] = "文件内容太短或为空，无法处理。"
            return
        
        tasks[task_id]['progress'] = 30
        
        # 处理文本
        try:
            logger.info("开始识别难词...")
            difficult_words = identify_difficult_words(text, level)
            logger.info(f"难词识别完成，找到 {len(difficult_words)} 个难词")
            
            tasks[task_id]['progress'] = 50
            
            logger.info("开始生成导读...")
            introduction = generate_introduction(text, level)
            logger.info("导读生成完成")
            
            tasks[task_id]['progress'] = 70
            
            logger.info("开始生成摘要和结构图...")
            summary_structure = generate_summary_and_structure(text, level)
            logger.info("摘要和结构图生成完成")
            
        except Exception as e:
            tasks[task_id]['status'] = 'failed'
            tasks[task_id]['error'] = f"AI处理文本时出错: {str(e)}"
            return
        
        tasks[task_id]['progress'] = 80
        
        # 创建标注后的PDF
        try:
            logger.info("开始生成PDF...")
            annotated_pdf = create_annotated_pdf(text, difficult_words, introduction, summary_structure, os.path.splitext(filename)[0])
            
            # 保存PDF到文件
            output_filename = f"{os.path.splitext(filename)[0]}_标注.pdf"
            output_path = os.path.join(PROCESSED_FOLDER, output_filename)
            
            with open(output_path, 'wb') as f:
                f.write(annotated_pdf.getvalue())
            
            logger.info(f"PDF已保存到: {output_path}")
            
            # 更新任务信息
            tasks[task_id]['status'] = 'completed'
            tasks[task_id]['progress'] = 100
            tasks[task_id]['result_file'] = output_filename
            
        except Exception as e:
            tasks[task_id]['status'] = 'failed'
            tasks[task_id]['error'] = f"生成PDF时出错: {str(e)}"
            return
            
    except Exception as e:
        tasks[task_id]['status'] = 'failed'
        tasks[task_id]['error'] = f"处理文档时发生错误: {str(e)}"
    finally:
        # 清理临时文件
        try:
            os.remove(file_path)
        except:
            pass

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_document():
    try:
        # 检查文件是否存在
        if 'file' not in request.files:
            return jsonify({
                'status': 'error',
                'message': '没有上传文件'
            }), 400
        
        file = request.files['file']
        
        # 检查文件名
        if file.filename == '':
            return jsonify({
                'status': 'error',
                'message': '未选择文件'
            }), 400
        
        # 记录文件信息
        logger.info(f"正在处理文件: {file.filename}, 大小: {request.content_length} 字节")
        
        # 检查文件扩展名
        if not allowed_file(file.filename):
            return jsonify({
                'status': 'error',
                'message': f"不支持的文件格式: {file.filename}。请上传PDF或DOCX文件。"
            }), 400
        
        level = request.form.get('level', '四级')  # 默认为四级
        logger.info(f"选择的英语水平: {level}")
        
        # 生成任务ID
        task_id = str(uuid.uuid4())
        
        # 创建临时文件
        safe_filename = hashlib.md5(file.filename.encode()).hexdigest() + os.path.splitext(file.filename)[1]
        file_path = os.path.join(UPLOAD_FOLDER, safe_filename)
        file.save(file_path)
        
        # 创建任务
        tasks[task_id] = {
            'id': task_id,
            'status': 'queued',
            'filename': file.filename,
            'level': level,
            'created_at': datetime.now().isoformat(),
            'progress': 0
        }
        
        # 启动异步处理任务
        thread = threading.Thread(
            target=process_document_task,
            args=(task_id, file_path, level, file.filename)
        )
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'status': 'success',
            'message': '文件已上传，正在处理',
            'task_id': task_id
        })
        
    except Exception as e:
        logger.error(f"上传处理异常: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"处理上传时发生错误: {str(e)}"
        }), 500

@app.route('/task/<task_id>', methods=['GET'])
def get_task_status(task_id):
    """获取任务状态"""
    if task_id not in tasks:
        return jsonify({
            'status': 'error',
            'message': '任务不存在'
        }), 404
    
    task = tasks[task_id]
    
    response = {
        'id': task['id'],
        'status': task['status'],
        'progress': task['progress'],
        'filename': task['filename'],
        'created_at': task['created_at']
    }
    
    if task['status'] == 'completed' and 'result_file' in task:
        response['result_file'] = task['result_file']
        response['download_url'] = url_for('download_file', filename=task['result_file'], _external=True)
    
    if task['status'] == 'failed' and 'error' in task:
        response['error'] = task['error']
    
    return jsonify(response)

@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    """下载处理完成的文件"""
    file_path = os.path.join(PROCESSED_FOLDER, filename)
    
    if not os.path.exists(file_path):
        return jsonify({
            'status': 'error',
            'message': '文件不存在'
        }), 404
    
    return send_file(
        file_path,
        as_attachment=True,
        download_name=filename,
        mimetype='application/pdf'
    )

@app.route('/test_api', methods=['GET'])
def test_api():
    try:
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "你好，请用中文回答：今天天气如何？"}],
            "temperature": 0.7,
            "max_tokens": 50
        }
        
        response = requests.post(
            DEEPSEEK_API_URL, 
            headers=headers, 
            json=data,
            timeout=10
        )
        
        if response.status_code == 200:
            return jsonify({
                "status": "success",
                "message": "API 连接正常",
                "response": response.json()
            })
        else:
            return jsonify({
                "status": "error",
                "message": f"API 返回错误: {response.status_code}",
                "response": response.text
            }), 500
            
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"API 测试失败: {str(e)}"
        }), 500

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({
        'status': 'error',
        'message': '文件太大，请上传小于10MB的文件。'
    }), 413

# 在程序开始时调用
if __name__ == '__main__':
    try:
        print("\n=== 调试信息 ===")
        print("1. 检查Python版本...")
        import sys
        print(f"Python版本: {sys.version}")
        
        print("\n2. 检查依赖...")
        check_dependencies()
        print("依赖检查完成")
        
        # 使用固定端口 8000
        port = 8000
        print(f"\n3. 将使用端口: {port}")
        
        print("\n=== 英语学习助手正在启动 ===")
        print(f"请在浏览器中访问：http://127.0.0.1:{port}")
        print("按 Ctrl+C 停止服务器")
        
        # 启动应用
        app.run(host='127.0.0.1', port=port, debug=True)
    except Exception as e:
        print(f"\n启动失败: {str(e)}")
        print("\n详细错误信息:")
        import traceback
        print(traceback.format_exc())
        input("\n按回车键退出...")  # 防止窗口立即关闭
