import re,os,sys,datetime
from datetime import timedelta
import requests
from pathlib import Path
import logging
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# 获取程序执行目录
def _get_executable_path():
    if getattr(sys, 'frozen', False):
        # 如果程序是被“冻结”打包的，使用这个路径
        return Path(sys.executable).parent.as_posix()
    else:
        return Path(__file__).parent.as_posix()


# 将srt文件或合法srt字符串转为字典对象
def get_subtitle_from_srt(srtfile, *, is_file=True):
    def _readfile(file):
        content=""
        try:
            with open(file,'r',encoding='utf-8') as f:
                content=f.read().strip()
        except Exception as e:
            try:
                with open(file,'r', encoding='gbk') as f:
                    content = f.read().strip()
            except Exception as e:
                logger.exception(e,exc_info=True)
        return content

    content=''
    if is_file:
        content=_readfile(srtfile)
    else:
        content = srtfile.strip()

    if len(content) < 1:
        raise Exception(f"srt is empty:{srtfile=},{content=}")

    result = format_srt(content)

    # txt 文件转为一条字幕
    if len(result) < 1:
        result = [
            {"line": 1, "time": "00:00:00,000 --> 00:00:02,000", "text": "\n".join(content)}
        ]
    return result
'''
格式化毫秒或秒为符合srt格式的 2位小时:2位分:2位秒,3位毫秒 形式
print(ms_to_time_string(ms=12030))
-> 00:00:12,030
'''
def ms_to_time_string(*, ms=0, seconds=None):
    # 计算小时、分钟、秒和毫秒
    if seconds is None:
        td = timedelta(milliseconds=ms)
    else:
        td = timedelta(seconds=seconds)
    hours, remainder = divmod(td.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    milliseconds = td.microseconds // 1000

    time_string = f"{hours}:{minutes}:{seconds},{milliseconds}"
    return format_time(time_string, ',')

# 将不规范的 时:分:秒,|.毫秒格式为  aa:bb:cc,ddd形式
# eg  001:01:2,4500  01:54,14 等做处理
def format_time(s_time="", separate=','):
    if not s_time.strip():
        return f'00:00:00{separate}000'
    hou, min, sec,ms = 0, 0, 0,0

    tmp = s_time.strip().split(':')
    if len(tmp) >= 3:
        hou,min,sec = tmp[-3].strip(),tmp[-2].strip(),tmp[-1].strip()
    elif len(tmp) == 2:
        min,sec = tmp[0].strip(),tmp[1].strip()
    elif len(tmp) == 1:
        sec = tmp[0].strip()
    
    if re.search(r',|\.', str(sec)):
        t = re.split(r',|\.', str(sec))
        sec = t[0].strip()
        ms=t[1].strip()
    else:
        ms = 0
    hou = f'{int(hou):02}'[-2:]
    min = f'{int(min):02}'[-2:]
    sec = f'{int(sec):02}'
    ms = f'{int(ms):03}'[-3:]
    return f"{hou}:{min}:{sec}{separate}{ms}"

# 将 datetime.timedelta 对象的秒和微妙转为毫秒整数值
def toms(td):
    return (td.seconds * 1000) + int(td.microseconds / 1000)

# 将 时:分:秒,毫秒 转为毫秒整数值
def get_ms_from_hmsm(time_str):
    h,m,sec2ms=0,0,'00,000'
    tmp0= time_str.split(":")
    if len(tmp0)==3:
        h,m,sec2ms=tmp0[0],tmp0[1],tmp0[2]
    elif len(tmp0)==2:
        m,sec2ms=tmp0[0],tmp0[1]
        
    tmp=sec2ms.split(',')
    ms=tmp[1] if len(tmp)==2 else 0
    sec=tmp[0]
    
    return int(int(h) * 3600000 + int(m) * 60000 +int(sec)*1000 + int(ms))

# 合法的srt字符串转为 dict list
def srt_str_to_listdict(content):
    import srt
    line=0
    result=[]
    for sub in srt.parse(content):
        line+=1
        it={
            "start_time":toms(sub.start),
            "end_time":toms(sub.end),
            "line":line,
            "text":sub.content
        }
        it['startraw']=ms_to_time_string(ms=it['start_time'])
        it['endraw']=ms_to_time_string(ms=it['end_time'])
        it["time"]=f"{it['startraw']} --> {it['endraw']}"
        result.append(it)
    return result
# 将普通文本转为合法的srt字符串
def process_text_to_srt_str(input_text:str):
    if is_srt_string(input_text):
       return input_text
       
    # 将文本按换行符切割成列表
    text_lines = [line.strip() for line in input_text.replace("\r","").splitlines() if line.strip()]
      
    # 分割大于50个字符的行
    text_str_list = []
    for line in text_lines:
        if len(line) > 50:
            # 按标点符号分割为多个字符串
            split_lines = re.split(r'[,.，。]', line)
            text_str_list.extend([l.strip() for l in split_lines if l.strip()])
        else:
            text_str_list.append(line)
    # 创建字幕字典对象列表
    dict_list = []
    start_time_in_seconds = 0  # 初始时间，单位秒

    for i, text in enumerate(text_str_list, start=1):
        # 计算开始时间和结束时间（每次增加1s）
        start_time = ms_to_time_string(seconds=start_time_in_seconds)
        end_time = ms_to_time_string(seconds=start_time_in_seconds + 1)
        start_time_in_seconds += 1

        # 创建字幕字典对象
        srt = f"{i}\n{start_time} --> {end_time}\n{text}"
        dict_list.append(srt)
    
    return "\n\n".join(dict_list)


# 将字符串或者字幕文件内容，格式化为有效字幕数组对象
# 格式化为有效的srt格式
def format_srt(content):
    result=[]
    try:
        result=srt_str_to_listdict(content)
    except Exception:
        result=srt_str_to_listdict(process_text_to_srt_str(content))        
    return result
    

# 将字幕字典列表写入srt文件
def save_srt(srt_list, srt_file):
    txt = get_srt_from_list(srt_list)
    with open(srt_file,"w", encoding="utf-8") as f:
        f.write(txt)
    return True

def get_current_time_as_yymmddhhmmss(format='hms'):
  """将当前时间转换为 YYMMDDHHmmss 格式的字符串。"""
  now = datetime.datetime.now()
  return now.strftime("%y%m%d%H%M%S" if format!='hms' else "%H%M%S")

# 从 字幕 对象中获取 srt 字幕串
def get_srt_from_list(srt_list):
    txt = ""
    line = 0
    # it中可能含有完整时间戳 it['time']   00:00:01,123 --> 00:00:12,345
    # 开始和结束时间戳  it['startraw']=00:00:01,123  it['endraw']=00:00:12,345
    # 开始和结束毫秒数值  it['start_time']=126 it['end_time']=678
    for it in srt_list:
        line += 1
        if "startraw" not in it:
            # 存在完整开始和结束时间戳字符串 时:分:秒,毫秒 --> 时:分:秒,毫秒
            if 'time' in it:
                startraw, endraw = it['time'].strip().split(" --> ")
                startraw = format_time(startraw.strip().replace('.', ','), ',')
                endraw = format_time(endraw.strip().replace('.', ','), ',')
            elif 'start_time' in it and 'end_time' in it:
                # 存在开始结束毫秒数值
                startraw = ms_to_time_string(ms=it['start_time'])
                endraw = ms_to_time_string(ms=it['end_time'])
            else:
                raise Exception(
                    f'字幕中不存在 time/startraw/start_time 任何有效时间戳形式')
        else:
            # 存在单独开始和结束  时:分:秒,毫秒 字符串
            startraw = it['startraw']
            endraw = it['endraw']
        txt += f"{line}\n{startraw} --> {endraw}\n{it['text']}\n\n"
    return txt

def runffmpeg(cmd):
    import subprocess
    try:
        subprocess.run(cmd,
                       stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE,
                       encoding="utf-8",
                       check=True,
                       text=True,
                       creationflags=0 if sys.platform != 'win32' else subprocess.CREATE_NO_WINDOW)
        
    except Exception as e:
        raise Exception(str(e.stderr) if hasattr(e,'stderr') and e.stderr else f'执行Ffmpeg操作失败:{cmd=}')
    return True
    


ROOT_DIR=Path(os.getcwd()).as_posix()
TMP_DIR=f'{ROOT_DIR}/tmp'
os.environ['PATH'] = ROOT_DIR + ';' + os.environ['PATH']
Path(f'{TMP_DIR}').mkdir(parents=True, exist_ok=True)
Path(f'{ROOT_DIR}/logs').mkdir(parents=True, exist_ok=True)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_file_handler = logging.FileHandler(f'{ROOT_DIR}/logs/{datetime.datetime.now().strftime("%Y%m%d")}.log', encoding='utf-8')
_file_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
_file_handler.setFormatter(formatter)
logger.addHandler(_file_handler)


safetySettings = [
    {
        "category": HarmCategory.HARM_CATEGORY_HARASSMENT,
        "threshold": HarmBlockThreshold.BLOCK_NONE,
    },
    {
        "category": HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        "threshold": HarmBlockThreshold.BLOCK_NONE,
    },
    {
        "category": HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        "threshold": HarmBlockThreshold.BLOCK_NONE,
    },
    {
        "category": HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        "threshold": HarmBlockThreshold.BLOCK_NONE,
    },
]
__all__=[
"ROOT_DIR",
"TMP_DIR",
"runffmpeg",
"get_subtitle_from_srt",
"logger",
"safetySettings"
]