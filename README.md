
# AI-trans-recogn

利用 AI大模型 三步反思法翻译字幕 & 音视频转录字幕


## 功能特性

- GeminiAI +三步反思法翻译SRT字幕
- GeminiAI + 音视频转录为SRT字幕

## UI

启动将在web浏览器打开一个单页 `http://127.0.0.1:5030`. 可执行翻译或转录操作

## 部署

Windows下载 预打包版，解压双击 `启动.bat`可用

Linux和Mac

```

git clone https://github.com/jianchang512/ai-trans-recogn 

cd ai-trans-recogn

python3 -m venv venv 


source ./venv/bin/activate

pip3 install -r requirements.txt


python3 app.py


```

