import os
import openai
from flask import Flask, request, abort

# LINE Bot SDK
from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage
)

app = Flask(__name__)

# ======== 環境変数 or 固定値で設定 =========
# 実運用では環境変数に設定してください
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "<Your Line Channel Access Token>")
LINE_CHANNEL_SECRET      = os.getenv("LINE_CHANNEL_SECRET", "<Your Line Channel Secret>")
OPENAI_API_KEY          = os.getenv("OPENAI_API_KEY", "<Your OpenAI API Key>")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)
openai.api_key = OPENAI_API_KEY

# --- ユーザーごとの状態を管理するための簡易辞書 ---
user_status = {}  # { user_id: { "in_question_mode": bool, "current_category": str } }


@app.route("/callback", methods=['POST'])
def callback():
    """ LINE Messaging API Webhook エンドポイント """
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text.strip()

    # ユーザー状態が未登録なら初期化
    if user_id not in user_status:
        user_status[user_id] = {
            "in_question_mode": False,
            "current_category": None
        }

    # (1) 「質問する」のトリガー
    if user_text == "質問する":
        user_status[user_id]["in_question_mode"] = True
        user_status[user_id]["current_category"] = None

        reply_text = "質問内容を入力してください。"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
        return

    # (2) 質問モードでない場合は何も返さず終了
    if not user_status[user_id]["in_question_mode"]:
        return

    # (3) 質問モード中の場合、ChatGPT にカテゴリを判定させる
    # まだカテゴリが決まっていなければ分類を実施
    if not user_status[user_id]["current_category"]:
        category = classify_question_by_chatgpt(user_text)
        user_status[user_id]["current_category"] = category
    else:
        # すでにカテゴリが決まっているなら、同じカテゴリを継続使用
        category = user_status[user_id]["current_category"]

    # カテゴリに応じてファイルを決定
    category_file = map_category_to_file(category)

    # prompt.txt と参照ファイルを合体させたプロンプトを作成
    prompt = build_prompt(category_file, user_text)

    # ChatGPT から応答を取得
    response_text = get_openai_response(prompt)

    # ユーザーに返信
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=response_text)
    )

    if user_text == "終了":
        user_status[user_id]["in_question_mode"] = False
        user_status[user_id]["current_category"] = None


def classify_question_by_chatgpt(question_text: str) -> str:
    """
    ChatGPT にカテゴリ選択をさせる関数
    - 「イベント」「スタッフルール」「給与・勤務」 のいずれかを返す
    """
    system_prompt = """
あなたはユーザーの質問のカテゴリを判定するアシスタントです。
以下のユーザーの質問が、「イベント」に関するものか、「スタッフルール」に関するものか、「給与・勤務」に関するものか、一つだけ選択してください。
必ず「イベント」「スタッフルール」「給与・勤務」のいずれかのみで答えてください。
それ以外の余分な文章は出力しないでください。
"""

    user_prompt = f"ユーザーの質問: {question_text}"

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.0,
        )
        classification = response["choices"][0]["message"]["content"].strip()

        # 万が一指定以外が返ってきたら、デフォルトで「イベント」にしておく
        if classification not in ["イベント", "スタッフルール", "給与・勤務"]:
            classification = "イベント"

        return classification

    except Exception as e:
        print(f"ChatGPT classification Error: {e}")
        # エラー時は暫定でイベントにする
        return "イベント"


def map_category_to_file(category: str) -> str:
    """
    カテゴリ名（「イベント」「スタッフルール」「給与・勤務」）を
    対応するファイル名にマッピングする
    """
    if category == "イベント":
        return "イベントについて.txt"
    elif category == "スタッフルール":
        return "スタッフルールについて.txt"
    elif category == "給与・勤務":
        return "給与・勤務について.txt"
    else:
        # 想定外の場合も一旦イベントにフォールバック
        return "イベントについて.txt"


def build_prompt(category_file: str, user_text: str) -> str:
    """
    prompt.txt と category_file の内容を合体させて最終的なプロンプトを作成
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # prompt.txt 読み込み
    prompt_file_path = os.path.join(base_dir, "prompt", "prompt.txt")
    with open(prompt_file_path, "r", encoding="utf-8") as f:
        prompt_base = f.read()

    # カテゴリ別の参照ファイルを読み込み
    category_file_path = os.path.join(base_dir, "prompt", category_file)
    with open(category_file_path, "r", encoding="utf-8") as f:
        ref_text = f.read()

    prompt = f"""{prompt_base}

{ref_text}

【ユーザーからの質問】
{user_text}
"""
    return prompt


def get_openai_response(prompt: str) -> str:
    """
    OpenAI ChatCompletion API から応答を取得
    """
    try:
        response = openai.ChatCompletion.create(
            model="o1-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        answer = response["choices"][0]["message"]["content"].strip()
        return answer
    except Exception as e:
        print(f"OpenAI API Error: {e}")
        return "申し訳ございません。現在、回答できません。時間をおいて再度お試しください。"


if __name__ == "__main__":
    # 本番運用時には適切なポートを設定
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)