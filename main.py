import os
import re
import requests
from typing import Tuple

from appwrite.client import Client
from appwrite.services.storage import Storage
from appwrite.services.functions import Functions
from appwrite.input_file import InputFile
from appwrite.services.tables_db import TablesDB
from appwrite.id import ID

from google import genai
import edge_tts

# Gemini client
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
DIFFBOT_TOKEN = os.getenv("DIFFBOT_TOKEN")

# Allowed origins for CORS
ALLOWED_ORIGINS = [
    "http://localhost:8080",
    "https://readright.appwrite.network",
]

def get_origin_header(origin: str) -> str:
    """Return allowed origin if valid, else empty string."""
    return origin if origin in ALLOWED_ORIGINS else ""


def fetch_article_text(url: str) -> Tuple[str, str]:
    """Fetch clean article text and title using Diffbot API."""
    api_url = f"https://api.diffbot.com/v3/article?token={DIFFBOT_TOKEN}&url={url}"
    try:
        response = requests.get(api_url, timeout=25)
        response.raise_for_status()
        data = response.json()
        obj = data.get("objects", [{}])[0]
        if response.status_code == 404:
            return "", ""
        return obj.get("text", ""), obj.get("title", "Untitled")
    except Exception:
        return "", ""


def generate_with_fallback(prompt: str) -> str:
    """Try Gemini 2.5 → 2.0 → 1.5 Flash with graceful fallback, raise if all fail."""
    models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    
    last_error = None
    for model in models:
        try:
            data = {"thinking_config": {"thinking_budget": -1}} if model == "gemini-2.5-flash" else {}
            result = gemini_client.models.generate_content(
                model=model, contents=prompt, config=data   
            )
            text = getattr(result, "text", "").strip()
            if text:
                return text
        except Exception as e:
            last_error = e
            continue
    
    raise RuntimeError("All Gemini models failed") from last_error


def generate_simplified_text(text: str) -> str:
    """Use Gemini to rewrite article in dyslexia-friendly format."""
    prompt = (
        "Rewrite this article to be dyslexia-friendly with large spacing and easy-to-read formatting. "
        "Do not add any headings, labels, or commentary. Only output the rewritten article:\n\n"
        f"{text}"
    )
    return generate_with_fallback(prompt)


def generate_tldr(text: str) -> str:
    """Use Gemini to summarize article into bullet points."""
    prompt = (
        "Summarize this article in concise bullet points only. "
        "Do not add any introduction or labels, just the bullets:\n\n"
        f"{text}"
    )
    return generate_with_fallback(prompt)

def generate_title(text: str) -> str:
    """Use Gemini to generate a title for the article."""
    prompt = (
        "Generate a concise and descriptive title for the following article only. "
        "Do not add any additional commentary or explanation. Just the title:\n\n"
        f"{text}"
    )
    result = gemini_client.models.generate_content(
        model="gemini-2.5-flash", contents=prompt, config={"thinking_config": {"thinking_budget": -1}}
    )
    return str(getattr(result, "text", ""))


def clean_text_for_tts(text: str) -> str:
    """Remove markdown and formatting artifacts before TTS."""
    text = re.sub(r'(\*\*|\*|__|_)', '', text)  # bold/italic
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)  # headings
    text = re.sub(r'[`>~]', '', text)  # code/blockquote
    return text


async def generate_tts(text: str, filename: str = "/tmp/audio.mp3") -> str:
    """Generate TTS audio file using Edge TTS."""
    try:
        cleaned_text = clean_text_for_tts(text)
        tts = edge_tts.Communicate(cleaned_text, voice="en-GB-LibbyNeural")
        await tts.save(filename)
        return filename
    except Exception:
        return ""


async def main(context):
    """Appwrite serverless function entrypoint with CORS."""
    origin = context.req.headers.get("origin", "")
    allowed_origin = get_origin_header(origin)

    try:
        # Handle CORS preflight
        if context.req.method == "OPTIONS":
            return context.res.send(
                "",
                200,
                headers={
                    "Access-Control-Allow-Origin": allowed_origin,
                    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization",
                },
            )

        # Init Appwrite client
        client = (
            Client()
            .set_project(os.environ["APPWRITE_FUNCTION_PROJECT_ID"])
            .set_key(context.req.headers["x-appwrite-key"])
        )
        storage = Storage(client)
        tablesDB = TablesDB(client)

        # ---- GET: check execution status ----
        if context.req.method == "GET":
            workerid = context.req.query.get("workerid")
            functions = Functions(client)
            response = functions.get_execution(
                function_id=os.environ["APPWRITE_FUNCTION_ID"],
                execution_id=workerid
            )
            return context.res.json(
                dict(response),  # ✅ ensure plain dict
                headers={"Access-Control-Allow-Origin": allowed_origin}
            )

        # ---- POST: process article or text ----
        data = context.req.body_json
        url, text = data.get("url"), data.get("text")

        if text:
            article_text = text
            title = generate_title(text)
        elif url:
            article_text, title = fetch_article_text(url)
            if not article_text:
                return context.res.send(
                    "",
                    404,
                    headers={"Access-Control-Allow-Origin": allowed_origin}
                )
        else:
            return context.res.send(
                "",
                400,
                headers={"Access-Control-Allow-Origin": allowed_origin}
            )

        docid = data.get("docid")

        # Generate outputs (always cast to str for safety)
        simplified = generate_simplified_text(article_text)
        tldr = generate_tldr(article_text)
        await generate_tts(simplified, filename="/tmp/audio.mp3")

        # Upload audio file
        file = storage.create_file(
            bucket_id=os.environ["APPWRITE_BUCKET_ID"],
            file_id=ID.unique(),
            file=InputFile.from_path("/tmp/audio.mp3"),
        )

        audio_link = (
            f"https://fra.cloud.appwrite.io/v1/storage/buckets/"
            f"{os.environ['APPWRITE_BUCKET_ID']}/files/{file['$id']}/view"
            f"?project={os.environ['APPWRITE_FUNCTION_PROJECT_ID']}"
        )

        # Save row to DB
        result_data = {
            "title": title,
            "simplifiedText": simplified,
            "tldr": tldr,
            "audioUrl": audio_link,
        }

        row = tablesDB.create_row(
            database_id=os.environ["APPWRITE_DATABASE_ID"],
            table_id=os.environ["APPWRITE_TABLE_ID"],
            row_id=docid,
            data=result_data,
        )

        return context.res.json(
            {"id": str(row["$id"])},
            headers={"Access-Control-Allow-Origin": allowed_origin}
        )

    except Exception as e:
        context.error(str(e))
        return context.res.send(
            "",
            500,
            headers={"Access-Control-Allow-Origin": allowed_origin}
        )
