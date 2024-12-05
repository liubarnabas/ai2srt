import asyncio
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
import edge_tts
import shutil
from pydub import AudioSegment

# 根据时间戳截取视频片段
from pydub.exceptions import CouldntDecodeError

from cfg import TMP_DIR, ROOT_DIR, logger


# 所有裁剪的视频片段合并后的原始短视频
CAIJIAN_HEBING = 'cai-hebing.mp4'

# 所有文案配音合并后的原始音频
PEIYIN_HEBING = 'peiyin-hebing.wav'


def create_short_video(video_path, time_list="", srt_str="", role="", pitch="+0Hz", rate="+0%", insert_srt=False):
    logger.debug(f"Entering create_short_video with video_path={video_path}, time_list={time_list}, srt_str={srt_str}, role={role}, pitch={pitch}, rate={rate}, insert_srt={insert_srt}")
    # 创建工作目录
    dirname = Path(video_path).parent.as_posix()
    logger.debug(f"Creating directory: {dirname}")
    Path(dirname).mkdir(parents=True, exist_ok=True)
    
    srt_file = f'{dirname}/subtitle.srt'
    logger.debug(f"Writing SRT string to file: {srt_file}")
    with open(srt_file, 'w', encoding='utf-8') as f:
        f.write(srt_str)
    
    # 根据时间片裁剪多个小片段
    t_list = time_list.strip().split(',')
    logger.debug(f"Parsed time list: {t_list}")
    file_list = []
    print(f'{t_list=}')
    logger.debug(f"Starting video cutting process")
    for i, it in enumerate(t_list):
        tmp = it.split('-')
        s = tmp[0]
        e = tmp[1]
        file_name = f'cai-{i}.mp4'
        file_list.append(f"file '{file_name}'")
        logger.debug(f"Cutting video segment {i}: start={s}, end={e}, output={dirname}/{file_name}")
        cut_from_video(source=video_path, ss=s, to=e, out=f'{dirname}/{file_name}')
    
    concat_txt_path = f'{dirname}/file.txt'
    logger.debug(f"Writing concat list to file: {concat_txt_path}")
    Path(concat_txt_path).write_text('\n'.join(file_list), encoding='utf-8')
    
    logger.debug(f"Concatenating video segments into {dirname}/{CAIJIAN_HEBING}")
    concat_multi_mp4(out=f'{dirname}/{CAIJIAN_HEBING}', concat_txt=concat_txt_path)

    # 开始配音
    logger.debug("Starting TTS creation")
    create_tts(srt_file=srt_file, dirname=dirname, role=role, rate=rate, pitch=pitch, insert_srt=insert_srt)
    
    try:
        logger.debug(f"Removing temporary concat file: {concat_txt_path}")
        Path(concat_txt_path).unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"Error removing concat file: {e}")
    
    logger.debug("Exiting create_short_video")


# 创建配音
def create_tts(*, srt_file, dirname, role="", rate='+0%', pitch="+0Hz", insert_srt=False):
    logger.debug(f"Entering create_tts with srt_file={srt_file}, dirname={dirname}, role={role}, rate={rate}, pitch={pitch}, insert_srt={insert_srt}")
    queue_tts = get_subtitle_from_srt(srt_file, is_file=True)
    logger.info(f'1 queue_tts={queue_tts}')
    for i, it in enumerate(queue_tts):
        queue_tts[i]['filename'] = f'{dirname}/peiyin-{i}.mp3'
    logger.info(f'2 queue_tts={queue_tts}')

    def _dubb(it):
        logger.debug(f"Starting dubbing thread for: {it}")
        async def _async_dubb(it):
            logger.debug(f"Async dubbing for text: {it['text']}")
            communicate_task = edge_tts.Communicate(
                text=it["text"],
                voice=role,
                rate=rate,
                proxy=None,
                pitch=pitch)
            await communicate_task.save(it['filename'])
            logger.debug(f"Saved TTS audio to {it['filename']}")
        
        try:
            asyncio.run(_async_dubb(it))
        except Exception as e:
            logger.error(f"Error in dubbing thread: {e}")

    split_queue = [queue_tts[i:i + 5] for i in range(0, len(queue_tts), 5)]
    logger.debug(f"Split queue into {len(split_queue)} batches")
    for items in split_queue:
        tasks = []
        for it in items:
            if it['text'].strip():
                tasks.append(threading.Thread(target=_dubb, args=(it,)))
        if len(tasks) < 1:
            logger.debug("No tasks to process in this batch")
            continue
        logger.debug(f"Starting {len(tasks)} dubbing threads")
        for t in tasks:
            t.start()
        for t in tasks:
            t.join()
        logger.debug("Completed dubbing threads for this batch")

    # 连接所有音频片段
    logger.debug("Starting to merge audio segments")
    for i, it in enumerate(queue_tts):
        the_ext = it['filename'].split('.')[-1]
        raw = it['end_time'] - it['start_time']
        if i > 0 and it['start_time'] < queue_tts[i-1]['end_time']:
            diff = queue_tts[i-1]['end_time'] - it['start_time'] + 50
            logger.debug(f"Adjusting timing for segment {i} by {diff}ms")
            it['start_time'] += diff
            it['end_time'] += diff
        # 存在配音文件
        if os.path.exists(it['filename']) and os.path.getsize(it['filename']) > 0:
            try:
                logger.debug(f"Loading audio file: {it['filename']}")
                seg_len = len(AudioSegment.from_file(it['filename'], format=the_ext))
                logger.debug(f"Segment length: {seg_len}ms, raw duration: {raw}ms")
                if seg_len > raw:
                    offset = seg_len - raw
                    logger.debug(f"Adjusting end_time by offset: {offset}ms")
                    it['end_time'] += offset
            except CouldntDecodeError as e:
                logger.error(f"Could not decode audio file {it['filename']}: {e}")
        queue_tts[i] = it
        logger.debug(f"Updated queue_tts[{i}] = {it}")

    merged_audio = AudioSegment.empty()
    logger.debug("Merging audio segments into a single track")
    for i, it in enumerate(queue_tts):
        if i == 0:
            if it['start_time'] > 0:
                logger.debug(f"Adding silence of {it['start_time']}ms before first audio segment")
                merged_audio += AudioSegment.silent(duration=it['start_time'])
        else:
            dur = it['start_time'] - queue_tts[i-1]['end_time']
            if dur > 0:
                logger.debug(f"Adding silence of {dur}ms between segments {i-1} and {i}")
                merged_audio += AudioSegment.silent(duration=dur)

        if os.path.isfile(it['filename']) and os.path.getsize(it['filename']) > 0:
            logger.debug(f"Adding audio file to merged_audio: {it['filename']}")
            merged_audio += AudioSegment.from_file(it['filename'], format="mp3")
        else:
            silence_duration = it['end_time'] - it['start_time']
            logger.debug(f"Adding silence of {silence_duration}ms for missing audio file at segment {i}")
            merged_audio += AudioSegment.silent(duration=silence_duration)

    srts = []
    logger.debug("Creating SRT entries for merged audio")
    for i, it in enumerate(queue_tts):
        srt_entry = f'{it["line"]}\n{ms_to_time_string(ms=it["start_time"])} --> {ms_to_time_string(ms=it["end_time"])}\n' + it["text"].replace('\n', '')
        srts.append(srt_entry)
        logger.debug(f"Created SRT entry: {srt_entry}")

    shutil.copy2(dirname+'/subtitle.srt', dirname+'/subtitle00.srt')
    logger.debug(f"Copied original subtitle to {dirname}/subtitle00.srt")
    Path(dirname+'/subtitle.srt').write_text('\n\n'.join(srts), encoding='utf-8')
    logger.debug("Updated subtitle.srt with merged SRT entries")

    # 计算时长
    audio_time = len(merged_audio)
    logger.debug(f"Merged audio duration: {audio_time}ms")
    # 获取视频的长度毫秒
    video_time = get_video_ms(f'{dirname}/{CAIJIAN_HEBING}')
    logger.debug(f"Video duration: {video_time}ms")
    if audio_time < video_time:
        logger.debug(f"Adding silence of {video_time - audio_time}ms to match video duration")
        merged_audio += AudioSegment.silent(duration=video_time - audio_time)

    merged_audio.export(f'{dirname}/{PEIYIN_HEBING}', format="wav")
    logger.debug(f"Exported merged audio to {dirname}/{PEIYIN_HEBING}")

    os.chdir(dirname)
    logger.debug(f"Changed working directory to {dirname}")

    tmp_wav = f"{dirname}/hunhe-{time.time()}.wav"
    yuan_wav = f'{dirname}/yuan.wav'
    logger.debug(f"Running ffmpeg to extract original audio to {yuan_wav}")
    runffmpeg(['-y', '-i', f'{dirname}/{CAIJIAN_HEBING}', '-vn', yuan_wav])
    
    ffmpeg_cmd = [
        '-y',
        '-i',
        yuan_wav,
        '-i',
        f'{dirname}/{PEIYIN_HEBING}',
        '-filter_complex',
        "[1:a]apad[a1];[0:a]volume=0.15[a0];[a0][a1]amerge=inputs=2[aout]",
        '-map',
        '[aout]',
        tmp_wav
    ]
    logger.debug(f"Running ffmpeg to merge audio tracks into {tmp_wav}")
    runffmpeg(ffmpeg_cmd)
    
    try:
        logger.debug(f"Removing temporary original audio file: {yuan_wav}")
        Path(yuan_wav).unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"Error removing original audio file: {e}")

    if insert_srt:
        logger.debug("Inserting subtitles into the final video")
        runffmpeg([
            "-y",
            "-i",
            f'{dirname}/{CAIJIAN_HEBING}',
            "-i",
            f'{tmp_wav}',
            "-i",
            f'subtitle.srt',
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:s",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            f"language=chi",
            '-af',
            'volume=1.8',
            "-shortest",  # 只处理最短的流（视频或音频）
            f'{dirname}/shortvideo.mp4'
        ])
    else:
        logger.debug("Merging audio without inserting subtitles into the final video")
        runffmpeg([
            "-y",
            "-i",
            f'{dirname}/{CAIJIAN_HEBING}',
            "-i",
            f'{tmp_wav}',
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            '-af',
            'volume=1.8',
            "-shortest",  # 只处理最短的流（视频或音频）
            f'{dirname}/shortvideo.mp4'
        ])
    
    os.chdir(ROOT_DIR)
    logger.debug(f"Changed working directory back to {ROOT_DIR}")
    
    try:
        logger.debug(f"Removing temporary merged audio file: {tmp_wav}")
        Path(tmp_wav).unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"Error removing temporary merged audio file: {e}")

    logger.debug("Exiting create_tts")


def runffprobe(cmd):
    logger.debug(f"Running ffprobe with command: {cmd}")
    try:
        if Path(cmd[-1]).is_file():
            cmd[-1] = Path(cmd[-1]).as_posix()
        p = subprocess.run(['ffprobe'] + cmd,
                           stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE,
                           encoding="utf-8",
                           text=True,
                           check=True,
                           creationflags=0 if sys.platform != 'win32' else subprocess.CREATE_NO_WINDOW)
        if p.stdout:
            logger.debug(f"ffprobe output: {p.stdout.strip()}")
            return p.stdout.strip()
        logger.error(f"ffprobe error: {p.stderr}")
        raise Exception(str(p.stderr))
    except Exception as e:
        logger.error(f"Exception in runffprobe: {e}")
        raise


# 获取视频信息
def get_video_ms(mp4_file):
    logger.debug(f"Getting video duration for file: {mp4_file}")
    mp4_file = Path(mp4_file).as_posix()
    out = runffprobe(
        ['-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', mp4_file])
    if out is False:
        logger.error('ffprobe error: did not get video information')
        raise Exception('ffprobe error: did not get video information')
    out = json.loads(out)
    logger.debug(f"ffprobe JSON output: {out}")
    if "streams" not in out or len(out["streams"]) < 1:
        logger.error('ffprobe error: streams is 0')
        raise Exception('ffprobe error: streams is 0')

    if "format" in out and out['format'].get('duration'):
        duration_ms = int(float(out['format']['duration']) * 1000)
        logger.debug(f"Video duration: {duration_ms}ms")
        return duration_ms
    logger.warning('ffprobe did not return duration')
    return 0


# 将字符串做 md5 hash处理
def get_md5(input_string: str):
    logger.debug(f"Generating MD5 for input string: {input_string}")
    md5 = hashlib.md5()
    md5.update(input_string.encode('utf-8'))
    md5_result = md5.hexdigest()
    logger.debug(f"MD5 result: {md5_result}")
    return md5_result


# 获取程序执行目录
def _get_executable_path():
    logger.debug("Getting executable path")
    if getattr(sys, 'frozen', False):
        # 如果程序是被“冻结”打包的，使用这个路径
        path = Path(sys.executable).parent.as_posix()
        logger.debug(f"Frozen executable path: {path}")
        return path
    else:
        path = Path(__file__).parent.as_posix()
        logger.debug(f"Script executable path: {path}")
        return path


# 将srt文件或合法srt字符串转为字典对象
def get_subtitle_from_srt(srtfile, *, is_file=True):
    logger.debug(f"Getting subtitles from {'file' if is_file else 'string'}: {srtfile}")
    def _readfile(file):
        logger.debug(f"Reading subtitle file: {file}")
        content = ""
        try:
            with open(file, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            logger.debug("Read subtitle file with utf-8 encoding")
        except Exception as e:
            logger.warning(f"Failed to read with utf-8 encoding: {e}")
            try:
                with open(file, 'r', encoding='gbk') as f:
                    content = f.read().strip()
                logger.debug("Read subtitle file with gbk encoding")
            except Exception as e:
                logger.error(f"Failed to read subtitle file with gbk encoding: {e}")
                raise
        return content

    content = ''
    if is_file:
        content = _readfile(srtfile)
    else:
        content = srtfile.strip()

    if len(content) < 1:
        logger.error(f"srt is empty: srtfile={srtfile}, content={content}")
        raise Exception(f"srt is empty: srtfile={srtfile}, content={content}")

    result = format_srt(content)
    logger.debug(f"Formatted subtitles: {result}")

    # txt 文件转为一条字幕
    if len(result) < 1:
        logger.debug("No valid subtitles found, creating a single subtitle entry")
        result = [
            {"line": 1,
             "time": "00:00:00,000 --> 00:00:02,000",
             "start_time": 0,
             "end_time": 2000,
             "text": "\n".join(content)}
        ]
    return result


'''
格式化毫秒或秒为符合srt格式的 2位小时:2位分:2位秒,3位毫秒 形式
print(ms_to_time_string(ms=12030))
-> 00:00:12,030
'''


def ms_to_time_string(*, ms=0, seconds=None):
    logger.debug(f"Converting to time string with ms={ms}, seconds={seconds}")
    # 计算小时、分钟、秒和毫秒
    if seconds is None:
        td = timedelta(milliseconds=ms)
    else:
        td = timedelta(seconds=seconds)
    hours, remainder = divmod(td.seconds, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    milliseconds = td.microseconds // 1000

    time_string = f"{hours}:{minutes}:{seconds_part},{milliseconds}"
    formatted_time = format_time(time_string, ',')
    logger.debug(f"Formatted time string: {formatted_time}")
    return formatted_time


# 将不规范的 时:分:秒,|.毫秒格式为  aa:bb:cc,ddd形式
# eg  001:01:2,4500  01:54,14 等做处理
def format_time(s_time="", separate=','):
    logger.debug(f"Formatting time string: s_time={s_time}, separate={separate}")
    if not s_time.strip():
        logger.debug("Empty time string provided, returning default")
        return f'00:00:00{separate}000'
    hou, min, sec, ms = 0, 0, 0, 0

    tmp = s_time.strip().split(':')
    if len(tmp) >= 3:
        hou, min, sec = tmp[-3].strip(), tmp[-2].strip(), tmp[-1].strip()
    elif len(tmp) == 2:
        min, sec = tmp[0].strip(), tmp[1].strip()
    elif len(tmp) == 1:
        sec = tmp[0].strip()

    if re.search(r',|\.', str(sec)):
        t = re.split(r',|\.', str(sec))
        sec = t[0].strip()
        ms = t[1].strip()
    else:
        ms = 0
    hou = f'{int(hou):02}'[-2:]
    min = f'{int(min):02}'[-2:]
    sec = f'{int(sec):02}'
    ms = f'{int(ms):03}'[-3:]
    formatted = f"{hou}:{min}:{sec}{separate}{ms}"
    logger.debug(f"Formatted time: {formatted}")
    return formatted


# 将 datetime.timedelta 对象的秒和微妙转为毫秒整数值
def toms(td):
    ms = (td.seconds * 1000) + int(td.microseconds / 1000)
    logger.debug(f"Converting timedelta to ms: {td} -> {ms}ms")
    return ms


# 将 时:分:秒,毫秒 转为毫秒整数值
def get_ms_from_hmsm(time_str):
    logger.debug(f"Converting time string to ms: {time_str}")
    h, m, sec2ms = 0, 0, '00,000'
    tmp0 = time_str.split(":")
    if len(tmp0) == 3:
        h, m, sec2ms = tmp0[0], tmp0[1], tmp0[2]
    elif len(tmp0) == 2:
        m, sec2ms = tmp0[0], tmp0[1]

    tmp = sec2ms.split(',')
    ms = tmp[1] if len(tmp) == 2 else 0
    sec = tmp[0]

    total_ms = int(int(h) * 3600000 + int(m) * 60000 + int(sec) * 1000 + int(ms))
    logger.debug(f"Converted {time_str} to {total_ms}ms")
    return total_ms


# 合法的srt字符串转为 dict list
def srt_str_to_listdict(content):
    import srt
    logger.debug("Parsing SRT string to list of dictionaries")
    line = 0
    result = []
    for sub in srt.parse(content):
        line += 1
        it = {
            "start_time": toms(sub.start),
            "end_time": toms(sub.end),
            "line": line,
            "text": sub.content
        }
        it['startraw'] = ms_to_time_string(ms=it['start_time'])
        it['endraw'] = ms_to_time_string(ms=it['end_time'])
        it["time"] = f"{it['startraw']} --> {it['endraw']}"
        result.append(it)
        logger.debug(f"Added subtitle entry: {it}")
    return result


# 判断是否是srt字符串
def is_srt_string(input_text):
    logger.debug("Checking if input text is a valid SRT string")
    input_text = input_text.strip()
    if not input_text:
        logger.debug("Input text is empty, not an SRT string")
        return False

    # 将文本按换行符切割成列表
    text_lines = input_text.replace("\r", "").splitlines()
    if len(text_lines) < 3:
        logger.debug("Input text has fewer than 3 lines, not an SRT string")
        return False

    # 正则表达式：第一行应为1到2个纯数字
    first_line_pattern = r'^\d{1,2}$'

    # 正则表达式：第二行符合时间格式
    second_line_pattern = r'^\s*?\d{1,2}:\d{1,2}:\d{1,2}(\W\d+)?\s*-->\s*\d{1,2}:\d{1,2}:\d{1,2}(\W\d+)?\s*$'

    # 如果前两行符合条件，返回原字符串
    if not re.match(first_line_pattern, text_lines[0].strip()) or not re.match(second_line_pattern, text_lines[1].strip()):
        logger.debug("First two lines do not match SRT patterns, not an SRT string")
        return False
    logger.debug("Input text matches SRT patterns")
    return True


# 将普通文本转为合法的srt字符串
def process_text_to_srt_str(input_text: str):
    logger.debug("Processing plain text to SRT string")
    if is_srt_string(input_text):
        logger.debug("Input text is already a valid SRT string")
        return input_text

    # 将文本按换行符切割成列表
    text_lines = [line.strip() for line in input_text.replace("\r", "").splitlines() if line.strip()]
    logger.debug(f"Split input text into lines: {text_lines}")

    # 分割大于50个字符的行
    text_str_list = []
    for line in text_lines:
        if len(line) > 50:
            # 按标点符号分割为多个字符串
            split_lines = re.split(r'[,.，。]', line)
            split_lines = [l.strip() for l in split_lines if l.strip()]
            logger.debug(f"Splitting long line into: {split_lines}")
            text_str_list.extend(split_lines)
        else:
            text_str_list.append(line)
    logger.debug(f"Processed text lines: {text_str_list}")

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
        logger.debug(f"Created SRT entry: {srt}")

    srt_str = "\n\n".join(dict_list)
    logger.debug(f"Final SRT string:\n{srt_str}")
    return srt_str


# 将字符串或者字幕文件内容，格式化为有效字幕数组对象
# 格式化为有效的srt格式
def format_srt(content):
    logger.debug("Formatting SRT content")
    result = []
    try:
        result = srt_str_to_listdict(content)
        logger.debug("Successfully parsed SRT content")
    except Exception as e:
        logger.warning(f"Failed to parse SRT content directly: {e}. Attempting to process as plain text")
        result = srt_str_to_listdict(process_text_to_srt_str(content))
    return result


# 将字幕字典列表写入srt文件
def save_srt(srt_list, srt_file):
    logger.debug(f"Saving SRT list to file: {srt_file}")
    txt = get_srt_from_list(srt_list)
    with open(srt_file, "w", encoding="utf-8") as f:
        f.write(txt)
    logger.debug("SRT file saved successfully")
    return True


def get_current_time_as_yymmddhhmmss(format='hms'):
    """将当前时间转换为 YYMMDDHHmmss 格式的字符串。"""
    now = datetime.datetime.now()
    time_format = "%y%m%d%H%M%S" if format != 'hms' else "%H%M%S"
    current_time = now.strftime(time_format)
    logger.debug(f"Current time formatted as {time_format}: {current_time}")
    return current_time


# 从 字幕 对象中获取 srt 字幕串
def get_srt_from_list(srt_list):
    logger.debug("Generating SRT string from list of subtitle dictionaries")
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
                logger.error('字幕中不存在 time/startraw/start_time 任何有效时间戳形式')
                raise Exception('字幕中不存在 time/startraw/start_time 任何有效时间戳形式')
        else:
            # 存在单独开始和结束  时:分:秒,毫秒 字符串
            startraw = it['startraw']
            endraw = it['endraw']
        srt_entry = f"{line}\n{startraw} --> {endraw}\n{it['text']}\n\n"
        txt += srt_entry
        logger.debug(f"Added SRT entry: {srt_entry}")
    return txt


def runffmpeg(cmd):
    logger.debug(f"Running ffmpeg with command: {cmd}")
    import subprocess
    try:
        if cmd[0] != 'ffmpeg':
            cmd.insert(0, 'ffmpeg')
        logger.info(f"ffmpeg command: {cmd}")
        subprocess.run(cmd,
                       stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE,
                       encoding="utf-8",
                       check=True,
                       text=True,
                       creationflags=0 if sys.platform != 'win32' else subprocess.CREATE_NO_WINDOW)
        logger.debug("ffmpeg command executed successfully")
    except Exception as e:
        error_message = str(e.stderr) if hasattr(e, 'stderr') and e.stderr else f'执行Ffmpeg操作失败: cmd={cmd}'
        logger.error(f"ffmpeg error: {error_message}")
        raise Exception(error_message)
    return True


# 从视频中切出一段时间的视频片段 cuda + h264_cuvid
def cut_from_video(*, ss="", to="", source="", out=""):
    logger.debug(f"Cutting video from {source}: ss={ss}, to={to}, out={out}")
    cmd1 = [
        "-y",
        "-ss",
        format_time(ss, '.')]
    if to != '':
        cmd1.append("-to")
        cmd1.append(format_time(to, '.'))  # 如果开始结束时间相同，则强制持续时间1s)
    cmd1.append('-i')
    cmd1.append(source)

    cmd = cmd1 + [f'{out}']
    logger.debug(f"ffmpeg cut_from_video command: {cmd}")
    result = runffmpeg(cmd)
    logger.debug(f"Completed cutting video to {out}")
    return result


# 创建 多个连接文件
def create_concat_txt(filelist, concat_txt=None):
    logger.debug(f"Creating concat text file from filelist: {filelist}, output: {concat_txt}")
    txt = []
    for it in filelist:
        if not Path(it).exists() or Path(it).stat().st_size == 0:
            logger.warning(f"File does not exist or is empty, skipping: {it}")
            continue
        txt.append(f"file '{os.path.basename(it)}'")
    if len(txt) < 1:
        logger.error('No valid files to concatenate')
        raise Exception('file list no valid')
    with Path(concat_txt).open('w', encoding='utf-8') as f:
        f.write("\n".join(txt))
        f.flush()
    logger.debug(f"Concat text file created at {concat_txt}")
    return concat_txt


# 多个视频片段连接 cuda + h264_cuvid
def concat_multi_mp4(*, out=None, concat_txt=None):
    logger.debug(f"Concatenating multiple MP4 files into {out} using {concat_txt}")
    os.chdir(os.path.dirname(concat_txt))
    logger.debug(f"Changed working directory to {os.path.dirname(concat_txt)}")
    runffmpeg(
        ['-y', '-f', 'concat', '-i', concat_txt, '-c:v', f"libx264", out])
    os.chdir(ROOT_DIR)
    logger.debug(f"Changed working directory back to {ROOT_DIR}")
    return True


def precise_speed_up_audio(*, file_path=None, target_duration_ms=120000, max_rate=100):
    logger.debug(f"Speeding up audio: file_path={file_path}, target_duration_ms={target_duration_ms}, max_rate={max_rate}")
    from pydub import AudioSegment
    audio = AudioSegment.from_file(file_path)
    logger.debug(f"Original audio duration: {len(audio)}ms")

    # 首先确保原时长和目标时长单位一致（毫秒）
    current_duration_ms = len(audio)
    logger.debug(f"Current duration (ms): {current_duration_ms}")
    # 计算速度变化率
    speedup_ratio = current_duration_ms / target_duration_ms
    logger.debug(f"Calculated speedup ratio: {speedup_ratio}")

    if target_duration_ms <= 0 or speedup_ratio <= 1:
        logger.debug("No speedup needed")
        return True
    rate = min(max_rate, speedup_ratio)
    logger.debug(f"Using speedup rate: {rate}")

    # 变速处理
    try:
        logger.debug("Applying speedup to audio")
        fast_audio = audio.speedup(playback_speed=rate)
        # 如果处理后的音频时长稍长于目标时长，进行剪裁
        if len(fast_audio) > target_duration_ms:
            logger.debug(f"Fast audio is longer than target. Trimming by {len(fast_audio) - target_duration_ms}ms")
            fast_audio = fast_audio[:target_duration_ms]
    except Exception as e:
        logger.error(f"Error speeding up audio: {e}. Trimming audio to target duration instead")
        fast_audio = audio[:target_duration_ms]

    fast_audio.export(file_path, format=file_path.split('.')[-1])
    logger.debug(f"Exported sped-up audio to {file_path}")
    # 返回速度调整后的音频
    return True
