import os
import json
import time
import re
import socket
from pathlib import Path
import logging

# Import necessary modules from google.generativeai
import google.generativeai as genai
from google.api_core.exceptions import ServerError, TooManyRequests, RetryError
from google.generativeai.types import RequestOptions
from google.api_core import retry

from cfg import ROOT_DIR, TMP_DIR, logger, safetySettings
import tools

# Ensure TMP_DIR exists
os.makedirs(TMP_DIR, exist_ok=True)

# Load 'prompt2.json'
with open(os.path.join(ROOT_DIR, 'prompt2.json'), 'r', encoding='utf-8') as f:
    PROMPT_LIST = json.load(f)
    logger.debug("Loaded prompt2.json successfully.")

class Gemini:
    def __init__(self, api_key, model_name='gemini-1.5-flash', audio_file=None):
        logger.debug(f"Initializing Gemini with api_key={'set' if api_key else 'not set'}, "
                     f"model_name={model_name}, audio_file={audio_file}")
        self.api_key = api_key
        self.model_name = model_name
        self.audio_file = audio_file

    def run_jieshuo(self):
        logger.debug("Starting run_jieshuo method.")
        video_filename = Path(self.audio_file).stem
        tmpname = f'{TMP_DIR}/{video_filename}.mp4'
        logger.debug(f"Temporary video file path for narration: {tmpname}")

        if not os.path.exists(tmpname):
            logger.debug(f"Temporary file {tmpname} does not exist. Running ffmpeg to generate it.")
            tools.runffmpeg(
                ['ffmpeg', '-y', '-i', self.audio_file, '-c:v', 'libx265', '-ac', '1', '-ar', '16000', '-preset',
                 'superfast', '-threads', '16', tmpname])
        else:
            logger.debug(f"Temporary file {tmpname} already exists. Skipping ffmpeg conversion.")

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
                logger.debug(f"Uploaded audio file for narration: {self.audio_file}")

                while sample_audio.state.name == "PROCESSING":
                    logger.debug("Audio file is still processing. Waiting...")
                    time.sleep(10)
                    sample_audio = genai.get_file(sample_audio.name)
                logger.debug("Audio file processing completed.")

                chat_session = model.start_chat(
                    history=[
                        {
                            "role": "user",
                            "parts": [sample_audio],
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

                time_match = re.search(r'<TIME>\**?(.*)\**?</TIME>', res_str, re.I | re.S)
                if time_match:
                    result['timelist'] = time_match.group(1).strip()
                    logger.debug(f"Extracted timelist: {result['timelist']}")

                srt_match = re.search(r'<SRT>\**?(.*)\**?</SRT>', res_str, re.I | re.S)
                if srt_match:
                    result["srt"] = srt_match.group(1).strip()
                    logger.debug(f"Extracted SRT: {result['srt']}")

                if not result['timelist'] or not result['srt']:
                    logger.error('Result is empty')
                    raise Exception('Result is empty')
                logger.debug("Narration and SRT extraction completed successfully.")
                return result
            except (ServerError, RetryError, socket.timeout) as e:
                logger.error("Unable to connect to Gemini, please try using or changing the proxy", exc_info=True)
                raise Exception('Unable to connect to Gemini, please try using or changing the proxy') from e
            except TooManyRequests as e:
                logger.error("429 Too Many Requests", exc_info=True)
                raise Exception('429 Too Many Requests') from e
            except Exception as e:
                logger.error("Exception occurred during narration:", exc_info=True)
                raise

if __name__ == '__main__':
    # Set up necessary variables
    API_KEY = os.environ.get('GEMINI_API_KEY')
    if not API_KEY:
        logger.error("API_KEY not found. Please set the GEMINI_API_KEY environment variable.")
        exit(1)

    MODEL_NAME = 'gemini-1.5-flash'
    ROLE = "zh-CN-YunxiNeural"    # Set to desired role or leave as None
    RATE = "+0%"
    PITCH= "+0Hz"
    INSERT_SRT = 0 # Set to 1 if you want to insert SRT

    INPUT_FOLDER = os.path.join(ROOT_DIR, 'input')
    OUTPUT_FOLDER = os.path.join(ROOT_DIR, 'output')

    # Ensure OUTPUT_FOLDER exists
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    # Get list of mp4 files in INPUT_FOLDER
    mp4_files = [os.path.join(INPUT_FOLDER, f) for f in os.listdir(INPUT_FOLDER) if f.endswith('.mp4')]

    logger.info(f"Found {len(mp4_files)} mp4 files in {INPUT_FOLDER}")

    for video_file in mp4_files:
        logger.info(f"Processing video file: {video_file}")
        try:
            # Initialize Gemini task
            task = Gemini(model_name=MODEL_NAME, api_key=API_KEY, audio_file=video_file)
            logger.debug("Initialized Gemini task for narration.")
            result = task.run_jieshuo()
            if not result:
                logger.warning("No narration script generated.")
                continue  # Skip to next file

            # Proceed to create short video
            logger.debug("Starting video processing based on timestamps.")
            tools.create_short_video(
                video_path=video_file,
                time_list=result['timelist'],
                srt_str=result['srt'],
                role=ROLE,
                pitch=PITCH,
                rate=RATE,
                insert_srt=INSERT_SRT
            )
            # Move or save the output video to OUTPUT_FOLDER
            output_video_path = os.path.join(OUTPUT_FOLDER, f"{Path(video_file)}_shortvideo.mp4")
            temp_output_video = os.path.join(INPUT_FOLDER, 'shortvideo.mp4')
            if os.path.exists(temp_output_video):
                os.rename(temp_output_video, output_video_path)
                logger.info(f"Saved output video to: {output_video_path}")
            else:
                logger.error(f"Expected output video not found: {temp_output_video}")
        except Exception as e:
            logger.exception(f"Error processing video file {video_file}: {str(e)}")

    logger.info("Batch processing completed.")
