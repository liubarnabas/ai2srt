"""
GeminiAI翻译字幕与转录音视频

@author https://pyvideotrans.com
"""
from pathlib import Path

HOST = '127.0.0.1'
PORT = 5030

import re, os

import socket

import google.generativeai as genai

from google.api_core.exceptions import ServerError, TooManyRequests, RetryError

import traceback
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
import threading, webbrowser, time
from waitress import serve
from google.generativeai.types import RequestOptions
from google.api_core import retry

import json
from cfg import ROOT_DIR, TMP_DIR, logger, safetySettings
import tools

app = Flask(__name__, template_folder=f'{ROOT_DIR}/templates', static_folder=os.path.join(ROOT_DIR, 'tmp'),
            static_url_path='/tmp')
CORS(app)


@app.route('/tmp/<path:filename>')
def static_files(filename):
    logger.debug(f"Serving static file: {filename}")
    return send_from_directory(app.config['STATIC_FOLDER'], filename)


with open(ROOT_DIR + "/prompt.json", 'r', encoding='utf-8') as f:
    PROMPT_LIST = json.loads(f.read())
    logger.debug("Loaded prompt.json successfully.")


class Gemini():

    def __init__(self, *, language=None, text="", api_key="", model_name='gemini-1.5-flash', piliang=50, waitsec=10,
                 audio_file=None):
        logger.debug(f"Initializing Gemini with language={language}, text length={len(text)}, "
                     f"api_key={'set' if api_key else 'not set'}, model_name={model_name}, "
                     f"piliang={piliang}, waitsec={waitsec}, audio_file={audio_file}")
        self.language = language

        self.srt_text = text
        self.api_key = api_key
        self.model_name = model_name
        self.piliang = piliang
        self.waitsec = waitsec
        self.audio_file = audio_file

    # 三步反思翻译srt字幕
    def run_trans(self):
        logger.debug("Starting run_trans method.")
        text_list = tools.get_subtitle_from_srt(self.srt_text, is_file=False)
        logger.debug(f"Retrieved {len(text_list)} subtitle entries.")
        split_source_text = [text_list[i:i + self.piliang] for i in range(0, len(text_list), self.piliang)]
        logger.debug(f"Split subtitles into {len(split_source_text)} batches of up to {self.piliang} entries each.")

        genai.configure(api_key=self.api_key)
        logger.debug("Configured genai with provided API key.")
        model = genai.GenerativeModel(self.model_name, safety_settings=safetySettings)
        logger.debug(f"Initialized GenerativeModel with model_name={self.model_name}.")

        result_str = ""
        req_nums = len(split_source_text)
        print(f'\n本次翻译将分 {req_nums} 次发送请求,每次发送 {self.piliang} 条字幕,可在 logs 目录下查看日志')
        logger.info(f"Starting translation with {req_nums} requests.")
        for i, it in enumerate(split_source_text):
            srt_str = "\n\n".join(
                [f"{srtinfo['line']}\n{srtinfo['time']}\n{srtinfo['text'].strip()}" for srtinfo in it])
            logger.debug(f"Processing batch {i+1}/{req_nums} with {len(it)} subtitles.")
            response = None

            try:
                prompt = PROMPT_LIST['prompt_trans'].replace('{lang}', self.language).replace('<INPUT></INPUT>',
                                                                                              f'<INPUT>{srt_str}</INPUT>')
                logger.debug(f"Constructed prompt for batch {i+1}.")

                print(f'开始发送请求 {i=}')
                logger.info(f"Sending request {i+1}/{req_nums} to Gemini API.")
                response = model.generate_content(
                    prompt,
                    safety_settings=safetySettings
                )
                logger.info(f'\n[Gemini]返回: response.text={response.text}')
                result_it = self._extract_text_from_tag(response.text)
                if not result_it:
                    start_line = i * self.piliang + 1
                    msg = (f"{start_line}->{(start_line + len(it))}行翻译结果出错{response.text}")
                    logger.error(msg)
                    result_str += msg.strip() + "\n\n"
                    continue
                result_str += result_it.strip() + "\n\n"
                logger.debug(f"Batch {i+1} translated successfully.")
            except (ServerError, RetryError, socket.timeout) as e:
                logger.error("无法连接到Gemini,请尝试使用或更换代理", exc_info=True)
                raise Exception('无法连接到Gemini,请尝试使用或更换代理') from e
            except TooManyRequests as e:
                logger.error("429请求太频繁", exc_info=True)
                raise Exception('429请求太频繁') from e
            except Exception as e:
                error = str(e)
                logger.error(f"Exception occurred: {error}", exc_info=True)
                if response and hasattr(response, 'prompt_feedback') and response.prompt_feedback.block_reason:
                    raise Exception(self._get_error(response.prompt_feedback.block_reason, "forbid")) from e

                if 'User location is not supported' in error or 'time out' in error:
                    raise Exception("当前请求ip(或代理服务器)所在国家不在Gemini API允许范围") from e

                if response and hasattr(response, 'candidates') and len(response.candidates) > 0:
                    candidate = response.candidates[0]
                    if candidate.finish_reason not in [0, 1]:
                        raise Exception(self._get_error(candidate.finish_reason)) from e
                    if candidate.finish_reason == 1 and candidate.content and hasattr(candidate.content, 'parts'):
                        result_it = self._extract_text_from_tag(response.text)
                        if not result_it:
                            raise Exception(f"翻译结果出错{response.text}") from e
                        result_str += result_it.strip() + "\n\n"
                        continue
                raise
            finally:
                if i < req_nums - 1:
                    print(f'请求 {i=} 结束，防止 429 错误， 暂停 {self.waitsec}s 后继续下次请求')
                    logger.debug(f"Sleeping for {self.waitsec} seconds to prevent 429 errors.")
                    time.sleep(self.waitsec)
        print(f'翻译结束\n\n')
        logger.info("Translation process completed.")
        return result_str

    # 转录音视频为字幕
    def run_recogn(self):
        logger.debug("Starting run_recogn method.")
        tmpname = f'{TMP_DIR}/{time.time()}.mp3'
        logger.debug(f"Temporary audio file created: {tmpname}")
        tools.runffmpeg(['ffmpeg', '-y', '-i', self.audio_file, '-ac', '1', '-ar', '8000', tmpname])
        self.audio_file = tmpname
        prompt = PROMPT_LIST['prompt_recogn']
        if self.language:
            prompt += PROMPT_LIST['prompt_recogn_trans'].replace('{lang}', self.language)
            logger.debug(f"Added translation prompt for language: {self.language}")

        result = []
        while True:
            try:
                genai.configure(api_key=self.api_key)
                logger.debug("Configured genai with provided API key for recognition.")
                model = genai.GenerativeModel(
                    self.model_name,
                    safety_settings=safetySettings
                )
                logger.debug(f"Initialized GenerativeModel with model_name={self.model_name} for recognition.")

                sample_audio = genai.upload_file(self.audio_file)
                logger.debug(f"Uploaded audio file: {self.audio_file}, response: {sample_audio}")

                response = model.generate_content([prompt, sample_audio], request_options={"timeout": 600})
                res_str = response.text.strip()
                logger.info(f"Recognition response: {res_str}")
                recogn_res = re.search(r'<RECONGITION>(.*)</RECONGITION>', res_str, re.I | re.S)
                if recogn_res:
                    result.append(recogn_res.group(1))
                    logger.debug("Extracted recognition result from response.")
                trans_res = re.search(r'<TRANSLATE>(.*)</TRANSLATE>', res_str, re.I | re.S)
                if trans_res:
                    result.append(trans_res.group(1))
                    logger.debug("Extracted translation result from response.")
                if not result:
                    logger.error('结果为空')
                    raise Exception('结果为空')
                logger.debug("Recognition and translation completed successfully.")
                return result
            except (ServerError, RetryError, socket.timeout) as e:
                logger.error("无法连接到Gemini,请尝试使用或更换代理", exc_info=True)
                raise Exception('无法连接到Gemini,请尝试使用或更换代理') from e
            except TooManyRequests as e:
                logger.warning("429请求太频繁，暂停60s后重试", exc_info=True)
                time.sleep(60)
                continue
            except Exception as e:
                logger.error("Exception occurred during recognition:", exc_info=True)
                raise
            finally:
                try:
                    Path(self.audio_file).unlink(missing_ok=True)
                    logger.debug(f"Removed temporary audio file: {self.audio_file}")
                except Exception as e:
                    logger.warning(f"Failed to remove temporary audio file: {self.audio_file}", exc_info=True)

    # 总结视频
    def run_zongjie(self):
        logger.debug("Starting run_zongjie method.")
        tmpname = f'{TMP_DIR}/{time.time()}.mp4'
        logger.debug(f"Temporary video file created for summarization: {tmpname}")
        tools.runffmpeg(
            ['ffmpeg', '-y', '-i', self.audio_file, '-c:v', 'libx265', '-ac', '1', '-ar', '16000', '-preset',
             'superfast', tmpname])
        self.audio_file = tmpname
        prompt = PROMPT_LIST['prompt_zongjie']
        logger.debug("Constructed summarization prompt.")
        result = ""
        while True:
            try:
                genai.configure(api_key=self.api_key)
                logger.debug("Configured genai with provided API key for summarization.")
                model = genai.GenerativeModel(
                    self.model_name,
                    safety_settings=safetySettings
                )
                logger.debug(f"Initialized GenerativeModel with model_name={self.model_name} for summarization.")

                sample_audio = genai.upload_file(self.audio_file)
                logger.debug(f"Uploaded audio file for summarization: {self.audio_file}, response: {sample_audio}")
                while sample_audio.state.name == "PROCESSING":
                    logger.debug("Audio file is still processing. Waiting...")
                    print('.', end='')
                    time.sleep(10)
                    sample_audio = genai.get_file(sample_audio.name)
                logger.debug("Audio file processing completed.")

                chat_session = model.start_chat(
                    history=[
                        {
                            "role": "user",
                            "parts": [
                                sample_audio,
                            ],
                        }
                    ])
                logger.debug("Started chat session for summarization.")
                response = chat_session.send_message(
                    prompt,
                    request_options=RequestOptions(
                        retry=retry.Retry(initial=10, multiplier=2, maximum=60, timeout=900),
                        timeout=900
                    )
                )
                result = response.text.strip()
                logger.info(f"Summarization response: {result}")
                return result
            except (ServerError, RetryError, socket.timeout) as e:
                logger.error("无法连接到Gemini,请尝试使用或更换代理", exc_info=True)
                raise Exception('无法连接到Gemini,请尝试使用或更换代理') from e
            except TooManyRequests as e:
                logger.error("429请求太频繁", exc_info=True)
                raise Exception('429请求太频繁') from e
            except Exception as e:
                logger.error("Exception occurred during summarization:", exc_info=True)
                raise
            finally:
                try:
                    Path(self.audio_file).unlink(missing_ok=True)
                    logger.debug(f"Removed temporary video file: {self.audio_file}")
                except Exception as e:
                    logger.warning(f"Failed to remove temporary video file: {self.audio_file}", exc_info=True)

    def run_jieshuo(self):
        logger.debug("Starting run_jieshuo method.")
        tmpname = f'{TMP_DIR}/{time.time()}.mp4'
        logger.debug(f"Temporary video file created for narration: {tmpname}")
        tools.runffmpeg(
            ['ffmpeg', '-y', '-i', self.audio_file, '-c:v', 'libx265', '-ac', '1', '-ar', '16000', '-preset',
             'superfast','-threads','16', tmpname])
        self.audio_file = tmpname
        prompt = PROMPT_LIST['prompt_jieshuo']
        logger.debug("Constructed narration prompt.")
        result = {"timelist": [], "srt": ""}
        while True:
            try:
                genai.configure(api_key=self.api_key)
                logger.debug("Configured genai with provided API key for narration.")
                model = genai.GenerativeModel(
                    self.model_name,
                    safety_settings=safetySettings
                )
                logger.debug(f"Initialized GenerativeModel with model_name={self.model_name} for narration.")

                sample_audio = genai.upload_file(self.audio_file)
                logger.debug(f"Uploaded audio file for narration: {self.audio_file}, response: {sample_audio}")
                while sample_audio.state.name == "PROCESSING":
                    logger.debug("Audio file is still processing. Waiting...")
                    print('.', end='')
                    time.sleep(10)
                    sample_audio = genai.get_file(sample_audio.name)
                logger.debug("Audio file processing completed.")

                chat_session = model.start_chat(
                    history=[
                        {
                            "role": "user",
                            "parts": [
                                sample_audio,
                            ],
                        }
                    ])
                logger.debug("Started chat session for narration.")
                response = chat_session.send_message(
                    prompt,
                    request_options=RequestOptions(
                        retry=retry.Retry(initial=10, multiplier=2, maximum=60, timeout=900),
                        timeout=900
                    )
                )

                res_str = response.text.strip()
                logger.info(f"Narration response: {res_str}")
                time_1 = re.search(r'<TIME>\**?(.*)\**?</TIME>', res_str, re.I | re.S)
                if time_1:
                    result['timelist'] = time_1.group(1).strip()
                    logger.debug(f"Extracted timelist: {result['timelist']}")

                srt_2 = re.search(r'<SRT>\**?(.*)\**?</SRT>', res_str, re.I | re.S)
                if srt_2:
                    result["srt"] = srt_2.group(1).strip()
                    logger.debug(f"Extracted SRT: {result['srt']}")
                if not result:
                    logger.error('结果为空')
                    raise Exception('结果为空')
                logger.debug("Narration and SRT extraction completed successfully.")
                return result
            except (ServerError, RetryError, socket.timeout) as e:
                logger.error("无法连接到Gemini,请尝试使用或更换代理", exc_info=True)
                raise Exception('无法连接到Gemini,请尝试使用或更换代理') from e
            except TooManyRequests as e:
                logger.error("429请求太频繁", exc_info=True)
                raise Exception('429请求太频繁') from e
            except Exception as e:
                logger.error("Exception occurred during narration:", exc_info=True)
                raise
            finally:
                try:
                    Path(self.audio_file).unlink(missing_ok=True)
                    logger.debug(f"Removed temporary video file: {self.audio_file}")
                except Exception as e:
                    logger.warning(f"Failed to remove temporary video file: {self.audio_file}", exc_info=True)

    def _extract_text_from_tag(self, text):
        logger.debug("Extracting text from response tags.")
        match = re.search(r'<step3_refined_translation>(.*?)</step3_refined_translation>', text, re.S)
        if match:
            extracted_text = match.group(1)
            logger.debug(f"Extracted text: {extracted_text}")
            return extracted_text
        else:
            logger.debug("No matching tag found for text extraction.")
            return ""

    def _get_error(self, num=5, type='error'):
        logger.debug(f"Retrieving error message for num={num}, type={type}.")
        REASON_CN = {
            2: "超出长度",
            3: "安全限制",
            4: "文字过度重复",
            5: "其他原因"
        }
        forbid_cn = {
            1: "被Gemini禁止翻译:出于安全考虑，提示已被屏蔽",
            2: "被Gemini禁止翻译:由于未知原因，提示已被屏蔽"
        }
        error_message = REASON_CN[num] if type == 'error' else forbid_cn[num]
        logger.debug(f"Error message retrieved: {error_message}")
        return error_message


@app.route('/')
def index():
    logger.debug("Serving index page.")
    return render_template(
        'index.html',
        prompt_trans=PROMPT_LIST['prompt_trans'],
        prompt_recogn=PROMPT_LIST['prompt_recogn'],
        prompt_recogn_trans=PROMPT_LIST['prompt_recogn_trans'],
        prompt_jieshuo=PROMPT_LIST['prompt_jieshuo'],
        prompt_zongjie=PROMPT_LIST['prompt_zongjie'],
    )


@app.route('/update_prompt', methods=['POST'])
def update_prompt():
    logger.debug("Received request to update prompt.")
    global PROMPT_LIST
    id = request.form.get('id')
    text = request.form.get('value')
    logger.debug(f"Updating prompt: id={id}, value={text}")
    PROMPT_LIST[id] = text
    with open(ROOT_DIR + "/prompt.json", 'w', encoding='utf-8') as f:
        json.dump(PROMPT_LIST, f, ensure_ascii=False)
        logger.debug("Updated prompt.json successfully.")
    return jsonify({"code": 0, "msg": "ok"})


@app.route('/upload', methods=['POST'])
def upload():
    logger.debug("Received request to upload audio file.")
    try:
        if 'audio' not in request.files:  # 检查是否上传了文件
            logger.warning("No file part in the request.")
            return jsonify({"code": 1, 'msg': 'No file part'})

        file = request.files['audio']
        if file.filename == '':  # 检查是否选择了文件
            logger.warning("No selected file in the request.")
            return jsonify({"code": 1, 'msg': 'No selected file'})

        # 获取文件扩展名
        file_ext = os.path.splitext(file.filename)[1]
        name = re.sub(r'["\'?,\[\]{}()`!@#$%\^&*+=\\;:><，。、？：；“”‘’—｛（）｝·|~]', '_', file.filename)
        # 使用时间戳生成文件名
        filename = f'{TMP_DIR}/{time.time()}{file_ext}'
        file.save(filename)
        logger.info(f"Uploaded audio file saved as {filename}.")
        return jsonify({'code': 0, 'msg': 'ok', 'data': filename})
    except Exception as e:
        logger.error("Error during file upload:", exc_info=True)
        return jsonify({"code": 1, 'msg': str(e)})


@app.route('/upload_video', methods=['POST'])
def upload_video():
    logger.debug("Received request to upload video file.")
    try:
        if 'audio' not in request.files:  # 检查是否上传了文件
            logger.warning("No file part in the video upload request.")
            return jsonify({"code": 1, 'msg': 'No file part'})

        file = request.files['audio']
        if file.filename == '':  # 检查是否选择了文件
            logger.warning("No selected file in the video upload request.")
            return jsonify({"code": 1, 'msg': 'No selected file'})

        # 获取文件扩展名
        name, file_ext = os.path.splitext(file.filename)
        name = re.sub(r'["\'?,\[\]{}()`!@#$%\^&*+=\\;:><，。、？：；“”‘’—｛（）｝·|~ \s]', '_', name.strip())
        # 使用时间戳生成文件名
        filename = name + "-" + tools.get_md5(file.filename)
        logger.debug(f"Generated filename for video: {filename}")
        # 创建目录
        target_dir = TMP_DIR + f'/{filename}'
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        file.save(f'{target_dir}/raw{file_ext}')
        logger.info(f"Saved raw video file to {target_dir}/raw{file_ext}.")
        # 保存文件到 /tmp 目录
        if file_ext.lower() != '.mp4':
            tools.runffmpeg(['-y', '-i', f'{target_dir}/raw{file_ext}', '-c:v', 'copy', f'{target_dir}/raw.mp4'])
            logger.info(f"Converted video to MP4 format: {target_dir}/raw.mp4")
        return jsonify({'code': 0, 'msg': 'ok', 'data': f'{target_dir}/raw.mp4'})
    except Exception as e:
        logger.error("Error during video upload:", exc_info=True)
        return jsonify({"code": 1, 'msg': str(e)})


def _checkparam(rate='0', pitch='0'):
    logger.debug(f"Checking parameters: rate={rate}, pitch={pitch}")
    try:
        pitch = int(pitch)
        pitch = f'+{pitch}' if pitch >= 0 else pitch
    except:
        pitch = '+0'
    pitch = f'{pitch}Hz'
    logger.debug(f"Processed pitch parameter: {pitch}")

    try:
        rate = int(rate)
        rate = f'+{rate}' if rate >= 0 else rate
    except:
        rate = '+0'
    rate = f'{rate}%'
    logger.debug(f"Processed rate parameter: {rate}")
    return rate, pitch


@app.route('/zongjie', methods=['POST'])
def zongjie():
    logger.debug("Received request for video summarization.")
    data = request.get_json()
    model_name = data.get('model_name')
    api_key = data.get('api_key')
    proxy = data.get('proxy')
    video_file = data.get('video_file')

    if not all([api_key]):  # Include audio_filename in the check
        logger.warning("API key not provided for summarization.")
        return jsonify({"code": 1, "msg": "必须输入api_key"})
    if not video_file:
        logger.warning("Video file not provided for summarization.")
        return jsonify({"code": 2, "msg": "视频文件必须要上传"})

    if proxy:
        os.environ['https_proxy'] = proxy
        logger.debug(f"Set HTTPS proxy to: {proxy}")
    try:
        task = Gemini(model_name=model_name, api_key=api_key, audio_file=video_file)
        logger.debug("Initialized Gemini task for summarization.")
        result = task.run_zongjie()
        if not result:
            logger.warning("No summary text generated.")
            return jsonify({"code": 3, "msg": '无总结文本生成'})

        logger.info("Summarization completed successfully.")
        return jsonify({"code": 0, "msg": "ok", "data": result})
    except Exception as e:
        logger.exception("Error during summarization:", exc_info=True)
        return jsonify({"code": 2, "msg": str(e)})


@app.route('/jieshuo', methods=['POST'])
def jieshuo():
    logger.debug("Received request for video narration.")
    data = request.get_json()
    model_name = data.get('model_name')
    api_key = data.get('api_key')
    proxy = data.get('proxy')
    video_file = data.get('video_file')
    role = data.get('role')
    rate = data.get('rate', 0)
    pitch = data.get('pitch', 0)
    autoend = int(data.get('autoend', 0))
    rate, pitch = _checkparam(rate, pitch)
    insert_srt = int(data.get('insert', 0))

    logger.debug(f"Parameters received for jieshuo: model_name={model_name}, api_key={'set' if api_key else 'not set'}, "
                 f"proxy={'set' if proxy else 'not set'}, video_file={video_file}, role={role}, "
                 f"rate={rate}, pitch={pitch}, autoend={autoend}, insert_srt={insert_srt}")

    if not all([api_key]):  # Include audio_filename in the check
        logger.warning("API key not provided for narration.")
        return jsonify({"code": 1, "msg": "必须输入api_key"})
    if not video_file:
        logger.warning("Video file not provided for narration.")
        return jsonify({"code": 2, "msg": "视频文件必须要上传"})

    if proxy:
        os.environ['https_proxy'] = proxy
        logger.debug(f"Set HTTPS proxy to: {proxy}")
    try:
        task = Gemini(model_name=model_name, api_key=api_key, audio_file=video_file)
        logger.debug("Initialized Gemini task for narration.")
        result = task.run_jieshuo()
        if not result:
            logger.warning("No narration script generated.")
            return jsonify({"code": 3, "msg": '无解说文案生成'})
        if autoend != 1:
            logger.debug("Autoend is not set to 1, returning narration result without video processing.")
            return jsonify({"code": 0, "msg": "ok", "data": result})

        # 开始根据时间戳截取视频
        logger.debug("Starting video processing based on timestamps.")
        tools.create_short_video(
            video_path=video_file,
            time_list=result['timelist'],
            srt_str=result['srt'],
            role=role,
            pitch=pitch,
            rate=rate,
            insert_srt=insert_srt
        )
        # 开始根据字幕配音
        video_url = '/tmp/' + str(Path(video_file).parent.stem) + '/shortvideo.mp4'
        logger.info(f"Video processing completed. Video URL: {video_url}")
        print(f'完成 {video_url=}')
        return jsonify({"code": 0, "msg": "ok", "data": result, "url": video_url})
    except Exception as e:
        logger.exception("Error during narration:", exc_info=True)
        return jsonify({"code": 2, "msg": str(e)})


@app.route('/gocreate', methods=['POST'])
def gocreate():
    logger.debug("Received request for creating short video with dubbed subtitles.")
    # 开始根据时间戳截取视频
    data = request.get_json()
    timelist = data.get('timelist')
    srt = data.get('srt')
    video_file = data.get('video_file')
    role = data.get('role')
    insert_srt = int(data.get('insert', 0))
    pitch = data.get('pitch', 0)
    rate = data.get('rate', 0)
    rate, pitch = _checkparam(rate, pitch)
    logger.debug(f"Parameters for gocreate: timelist={timelist}, srt length={len(srt)}, video_file={video_file}, "
                 f"role={role}, insert_srt={insert_srt}, pitch={pitch}, rate={rate}")
    print(f'{rate=},{pitch=}')
    try:
        tools.create_short_video(
            video_path=video_file,
            time_list=timelist,
            srt_str=srt,
            role=role,
            pitch=pitch,
            rate=rate,
            insert_srt=insert_srt
        )
        # 开始根据字幕配音
        video_url = '/tmp/' + str(Path(video_file).parent.stem) + '/shortvideo.mp4'
        logger.info(f"Short video created successfully. Video URL: {video_url}")
        print('完成')
        return jsonify({"code": 0, "msg": "ok", "url": video_url})
    except Exception as e:
        logger.error("Error during gocreate:", exc_info=True)
        import traceback
        print(traceback.format_exc())
        return jsonify({"code": 1, "msg": str(e)})


@app.route('/api', methods=['POST'])
def api():
    logger.debug("Received API request.")
    data = request.get_json()
    text = data.get('text')
    language = data.get('language')
    model_name = data.get('model_name')
    api_key = data.get('api_key')
    proxy = data.get('proxy')
    audio_file = data.get('audio_file')

    logger.debug(f"API parameters: text_present={'Yes' if text else 'No'}, language={language}, "
                 f"model_name={model_name}, api_key={'set' if api_key else 'not set'}, "
                 f"proxy={'set' if proxy else 'not set'}, audio_file={audio_file}")

    if not all([api_key]):  # Include audio_filename in the check
        logger.warning("API key not provided in API request.")
        return jsonify({"code": 1, "msg": "必须输入api_key"})
    if not text and not audio_file:
        logger.warning("Neither text nor audio_file provided in API request.")
        return jsonify({"code": 2, "msg": "srt字幕文件和音视频文件必须要选择一个"})

    if proxy:
        os.environ['https_proxy'] = proxy
        logger.debug(f"Set HTTPS proxy to: {proxy}")
    try:
        # logger.info(f'[API] 请求数据 {data=}')
        if text:
            logger.debug("Processing text translation via API.")
            task = Gemini(text=text, language=language, model_name=model_name, api_key=api_key)
            result = task.run_trans()
            if not result:
                logger.warning("No translation result obtained from API.")
                return jsonify({"code": 3, "msg": '无翻译结果'})
            logger.info("Text translation completed successfully.")
            return jsonify({"code": 0, "msg": "ok", "data": result})
        # 视频转录
        logger.debug("Processing audio/video recognition via API.")
        task = Gemini(text='', language=None if not language or language == '' else language, model_name=model_name,  api_key=api_key, audio_file=audio_file)
        result = task.run_recogn()
        if not result:
            logger.warning("No recognition result obtained from API.")
            return jsonify({"code": 3, "msg": '没有识别出字幕'})
        logger.info("Audio/video recognition completed successfully.")
        return jsonify({"code": 0, "msg": "ok", "data": result})
    except Exception as e:
        logger.exception("Error during API processing:", exc_info=True)
        return jsonify({"code": 2, "msg": str(e)})


def openurl(url):
    logger.debug(f"Preparing to open URL in web browser: {url}")
    def op():
        time.sleep(5)
        try:
            webbrowser.open_new_tab(url)
            logger.debug(f"Opened URL in web browser: {url}")
        except Exception as e:
            logger.error(f"Failed to open URL in web browser: {url}", exc_info=True)

    threading.Thread(target=op).start()


if __name__ == '__main__':
    try:
        logger.info(f"Starting Flask app on http://{HOST}:{PORT}")
        print(f"api接口地址  http://{HOST}:{PORT}")
        openurl(f'http://{HOST}:{PORT}')
        serve(app, host=HOST, port=PORT)
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}", exc_info=True)
        logger.error(traceback.format_exc())
