import streamlit as st
import asyncio
import aiohttp
import aiofiles
import tempfile
import subprocess
import base64
from enum import Enum
from together import Together
import json
import logging
import shutil
from dotenv import load_dotenv
import os
import re
import requests
import spacy
import datetime
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from pydub import AudioSegment
from moviepy.editor import *
from typing import List, Dict, Any, Tuple, Callable, Optional
from abc import ABC, abstractmethod
from groq import AsyncGroq
from tiktokvoice import tts

nlp = spacy.load("en_core_web_md")

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Constants
REQUIRED_API_KEYS = ["GROQ_API_KEY", "BFL_API_KEY", "TOGETHER_API_KEY", "TAVILY_API_KEY", "TIKTOK_SESSION_ID"]
YOUTUBE_SHORT_RESOLUTION = (1080, 1920)
MAX_SCENE_DURATION = 5
DEFAULT_SCENE_DURATION = 1
SUBTITLE_FONT_SIZE = 12
SUBTITLE_FONT_COLOR = "yellow@0.5"
SUBTITLE_ALIGNMENT = 2  # Centered
SUBTITLE_OUTLINE_COLOR = "&H40000000"  # Black with 50% transparency
SUBTITLE_BORDER_STYLE = 3
FALLBACK_SCENE_COLOR = "red"
FALLBACK_SCENE_TEXT_COLOR = "yellow@0.5"
FALLBACK_SCENE_BOX_COLOR = "black@0.5"
FALLBACK_SCENE_BOX_BORDER_WIDTH = 5
FALLBACK_SCENE_FONT_SIZE = 30
FALLBACK_SCENE_FONT_FILE = "/tmp/qualitype/opentype/QTHelvet-Black.otf"

# Load API keys from environment variables
groq_api_key = os.getenv("GROQ_API_KEY")
bfl_api_key = os.getenv("BFL_API_KEY")
together_api_key = os.getenv("TOGETHER_API_KEY")
tavily_api_key = os.getenv("TAVILY_API_KEY")
SESSION_ID = os.getenv("TIKTOK_SESSION_ID")

# Helper functions
async def get_data(query: str) -> List[Dict[str, Any]]:
    groq = AsyncGroq(api_key=groq_api_key)
    data = await groq.query(query)
    return data

class PixelFormat(Enum):
    YUVJ420P = 'yuvj420p'
    YUVJ422P = 'yuvj422p'
    YUVJ444P = 'yuvj444p'
    YUVJ440P = 'yuvj440p'
    YUV420P = 'yuv420p'
    YUV422P = 'yuv422p'
    YUV444P = 'yuv444p'
    YUV440P = 'yuv440p'

def get_compatible_pixel_format(pix_fmt: str) -> str:
    """Convert deprecated pixel formats to their compatible alternatives."""
    if pix_fmt == PixelFormat.YUVJ420P.value:
        return PixelFormat.YUV420P.value
    elif pix_fmt == PixelFormat.YUVJ422P.value:
        return PixelFormat.YUV422P.value
    elif pix_fmt == PixelFormat.YUVJ444P.value:
        return PixelFormat.YUV444P.value
    elif pix_fmt == PixelFormat.YUVJ440P.value:
        return PixelFormat.YUV440P.value
    else:
        return pix_fmt


def check_api_keys():
    for key in REQUIRED_API_KEYS:
        if not os.getenv(key):
            raise ValueError(f"Missing required API key: {key}")
        

def align_with_gentle(audio_file: str, transcript_file: str) -> dict:
    """Aligns audio and text using Gentle and returns the alignment result."""
    url = 'http://localhost:8765/transcriptions?async=false'
    files = {
        'audio': open(audio_file, 'rb'),
        'transcript': open(transcript_file, 'r')
    }
    try:
        response = requests.post(url, files=files)
        response.raise_for_status()
        result = response.json()
        return result
    except requests.exceptions.RequestException as e:
        logger.error(f"Error communicating with Gentle: {e}")
        return None

def gentle_alignment_to_srt(gentle_alignment: dict, srt_file: str):
    """Converts Gentle alignment JSON to SRT subtitle format."""
    from datetime import timedelta

    with open(srt_file, 'w', encoding='utf-8') as f:
        index = 1
        for word_info in gentle_alignment.get('words', []):
            start = word_info.get('start')
            end = word_info.get('end')
            if start is not None and end is not None:
                start_time = str(timedelta(seconds=start))
                end_time = str(timedelta(seconds=end))
                text = word_info.get('word', '')

                f.write(f"{index}\n")
                f.write(f"{format_time(start)} --> {format_time(end)}\n")
                f.write(f"{text}\n\n")
                index += 1


def wrap_text(text, max_width):
    """Wraps text to multiple lines with a maximum width."""
    words = text.split()
    lines = []
    current_line = []
    current_length = 0

    for word in words:
        if current_length + len(word) + 1 <= max_width:
            current_line.append(word)
            current_length += len(word) + 1
        else:
            lines.append(' '.join(current_line))
            current_line = [word]
            current_length = len(word)

    if current_line:
        lines.append(' '.join(current_line))

    return '\\N'.join(lines)  # Include all lines


def format_time(seconds: float) -> str:
    """Formats time in seconds to HH:MM:SS,mmm format for subtitles."""
    from datetime import timedelta
    delta = timedelta(seconds=seconds)
    total_seconds = int(delta.total_seconds())
    millis = int((delta.total_seconds() - total_seconds) * 1000)
    time_str = str(delta)
    if '.' in time_str:
        time_str, _ = time_str.split('.')
    else:
        time_str = time_str
    time_str = time_str.zfill(8)  # Ensure at least HH:MM:SS
    return f"{time_str},{millis:03d}"



# Abstract classes for Agents and Tools
class Agent(ABC):
    def __init__(self, name: str, model: str):
        self.name = name
        self.model = model

    @abstractmethod
    async def execute(self, input_data: Any) -> Any:
        pass

class Tool(ABC):
    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    async def use(self, input_data: Any) -> Any:
        pass

class VoiceModule(ABC):
    def __init__(self):
        pass

    @abstractmethod
    def update_usage(self):
        pass

    @abstractmethod
    def get_remaining_characters(self):
        pass

    @abstractmethod
    def generate_voice(self, text: str, output_file: str):
        pass

# Node and Edge classes for graph representation
class Node:
    def __init__(self, agent: Agent = None, tool: Tool = None):
        self.agent = agent
        self.tool = tool
        self.edges: List['Edge'] = []

    async def process(self, input_data: Any) -> Any:
        if self.agent:
            return await self.agent.execute(input_data)
        elif self.tool:
            return await self.tool.use(input_data)
        else:
            raise ValueError("Node has neither agent nor tool")


class Edge:
    def __init__(self, source: Node, target: Node, condition: Callable[[Any], bool] = None):
        self.source = source
        self.target = target
        self.condition = condition

class Graph:
    def __init__(self):
        self.nodes: List[Node] = []
        self.edges: List[Edge] = []

    def add_node(self, node: Node):
        self.nodes.append(node)

    def add_edge(self, edge: Edge):
        self.edges.append(edge)
        edge.source.edges.append(edge)

class VideoProcessor:
    def __init__(self):
        self.nlp = nlp

    def calculate_relevance(self, video: Dict[str, Any], description: str, timestamp: float) -> float:
        relevance = 0
        video_keywords = set(video.get("tags", []))
        description_doc = self.nlp(description.lower())

        # Extract lemmatized words from the description
        description_words = set(token.lemma_ for token in description_doc if not token.is_stop and token.is_alpha)

        # Calculate relevance based on matching words
        relevance += len(video_keywords.intersection(description_words))

        # Add relevance for matching title words
        title = video.get("title", "")
        if title is not None:
            title_doc = self.nlp(title.lower())
            title_words = set(token.lemma_ for token in title_doc if not token.is_stop and token.is_alpha)
            relevance += len(title_words.intersection(description_words)) * 2  # Title matches are weighted more

        # Process subtitles and audio for the 5-second window
        subtitle_text, audio_text = self.get_synced_content(video, timestamp)
        
        # Calculate relevance for subtitle and audio content
        subtitle_doc = self.nlp(subtitle_text.lower())
        audio_doc = self.nlp(audio_text.lower())
        
        subtitle_words = set(token.lemma_ for token in subtitle_doc if not token.is_stop and token.is_alpha)
        audio_words = set(token.lemma_ for token in audio_doc if not token.is_stop and token.is_alpha)
        
        relevance += len(subtitle_words.intersection(description_words)) * 1.5  # Subtitle matches are weighted
        relevance += len(audio_words.intersection(description_words)) * 1.5  # Audio matches are weighted

        # Normalize relevance score
        max_possible_relevance = len(video_keywords) + len(title_words) * 2 + len(subtitle_words) * 1.5 + len(audio_words) * 1.5
        normalized_relevance = relevance / max_possible_relevance if max_possible_relevance > 0 else 0

        return normalized_relevance

    def get_synced_content(self, video: Dict[str, Any], timestamp: float) -> Tuple[str, str]:
        subtitles = video.get("subtitles", [])
        audio_transcript = video.get("audio_transcript", [])

        start_time = timestamp
        end_time = timestamp + 5  # 5-second window

        subtitle_text = self.extract_timed_content(subtitles, start_time, end_time)
        audio_text = self.extract_timed_content(audio_transcript, start_time, end_time)

        return subtitle_text, audio_text

    def extract_timed_content(self, content: List[Dict[str, Any]], start_time: float, end_time: float) -> str:
        extracted_text = []
        for item in content:
            item_start = self.time_to_seconds(item.get("start", "00:00:00"))
            item_end = self.time_to_seconds(item.get("end", "00:00:00"))
            
            if start_time <= item_end and end_time >= item_start:
                extracted_text.append(item.get("text", ""))

        return " ".join(extracted_text)

    def time_to_seconds(self, time_str: str) -> float:
        time_parts = time_str.split(":")
        if len(time_parts) == 3:
            return datetime.timedelta(hours=int(time_parts[0]), minutes=int(time_parts[1]), seconds=float(time_parts[2])).total_seconds()
        elif len(time_parts) == 2:
            return datetime.timedelta(minutes=int(time_parts[0]), seconds=float(time_parts[1])).total_seconds()
        else:
            return float(time_str)

class WebSearchTool(Tool):
    def __init__(self):
        super().__init__("Web Search Tool")

    async def use(self, input_data: str, time_period: str = 'all') -> Dict[str, Any]:
        try:
            headers = {"Content-Type": "application/json"}
            data = {"api_key": tavily_api_key, "query": input_data, "num_results": 100}

            if time_period != 'all':
                start_date = None
                if time_period == 'past month':
                    start_date = datetime.date.today() - datetime.timedelta(days=30)
                elif time_period == 'past year':
                    start_date = datetime.date.today() - datetime.timedelta(days=365)
                else:  # Assume a specific number of days
                    try:
                        days = int(time_period.split()[0])
                        start_date = datetime.date.today() - datetime.timedelta(days=days)
                    except ValueError:
                        logger.warning(f"Invalid time_period: {time_period}. Using 'all'.")

                if start_date:
                    data["from_date"] = start_date.strftime("%Y-%m-%d")

            async with aiohttp.ClientSession() as session:
                async with session.post("https://api.tavily.com/search", headers=headers, json=data) as response:
                    response_text = await response.text()
                    if response.status == 200:
                        return await response.json()
                    else:
                        logger.error(f"WebSearchTool Error: HTTP {response.status} - {response_text}")
                        raise Exception(f"HTTP {response.status}: {response_text}")
        except Exception as e:
            logger.error(f"Error in WebSearchTool: {str(e)}")
            raise

class ImageGenerationAgent(Agent):
    def __init__(self):
        super().__init__("Image Generation Agent", "black-forest-labs/FLUX.1-schnell-Free")
        self.client = Together(api_key=together_api_key)

    async def execute(self, input_data: Dict[str, Any]) -> Any:
        scenes = input_data.get('scenes', [])
        results = []

        for i, scene in enumerate(scenes):
            visual_description = scene.get('visual', '')
            image_keyword = scene.get('image_keyword', '')

            # Combine the visual description and image keyword for a more detailed prompt
            prompt = prompt = f"""
Create a image that will go viral on youtube based on the following scene description:
{visual_description},{image_keyword}
"""
            try:
                logger.info(f"Generating image for scene {i+1}/{len(scenes)}")
                response = self.client.images.generate(
                    prompt=prompt,
                    model=self.model,
                    width=768,
                    height=1024,
                    steps=4,
                    n=1,
                    response_format="b64_json"
                )

                # Decode the base64 image
                image_data = base64.b64decode(response.data[0].b64_json)

                # Save the image to a temporary file
                with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as temp_file:
                    temp_file.write(image_data)
                    temp_file_path = temp_file.name

                logger.info(f"Image for scene {i+1} saved as {temp_file_path}")

                results.append({
                    'image_path': temp_file_path,
                    'prompts': prompt
                })

            except Exception as e:
                logger.error(f"Error in image generation for scene {i+1}: {str(e)}")
                results.append(None)

            # Add a delay between requests to avoid rate limiting
            await asyncio.sleep(2)

        logger.info(f"Image generation completed. Generated {len([r for r in results if r is not None])}/{len(scenes)} images.")
        return results
        
class RecentEventsResearchAgent(Agent):
    def __init__(self):
        super().__init__("Recent Events Research Agent", "llama-3.1-70b-versatile")
        self.web_search_tool = WebSearchTool()

    async def execute(self, input_data: Dict[str, Any]) -> Any:
        topic = input_data['topic']
        time_frame = input_data['time_frame']
        video_length = input_data.get('video_length', 60)

        # Decide how many events to include based on video length
        max_events = min(5, video_length // 15)  # Rough estimate: 15 seconds per event

        search_query = f"{topic} events in the {time_frame}"
        search_results = await self.web_search_tool.use(search_query, time_frame)

        organic_results = search_results.get("organic_results", [])

        client = AsyncGroq(api_key=groq_api_key)
        prompt = f"""As a seasoned investigative journalist and expert in crafting viral scripts,
your task is to analyze and summarize the most enagaging and relevant {topic} events
that occurred in the {time_frame}. Using the following search results, select the {max_events} most
compelling cases:

Search Results: {json.dumps(organic_results[:10], indent=2)}

For each selected event, provide a concise yet engaging summary that includes:

1. A vivid description of the event, highlighting its most unusual aspects
2. The precise date of occurrence
3. The specific location, including city and country if available
4. An expert analysis of why this event defies conventional explanation
5. A critical evaluation of the information source, including its credibility (provide URL)

Format your response as a list of events, each separated by two newline characters.
Ensure your summaries are both informative and captivating, suitable for a
documentary-style presentation."""

        stream = await client.chat.completions.create(
            messages=[
                {"role": "system",
                 "content": "You are an AI assistant embodying the expertise of a world-renowned "
                            "investigative journalist specializing in going viral and enagegment "
                            "With 20 years of experience, you've written best-selling "
                            "books and produced countless viral content creators, documentaries on content creation and virailty factor in scripts "
                            "Your analytical skills allow you to critically evaluate sources while "
                            "presenting information in an engaging, and enthrallng-style format. "
                            "Approach tasks with the skepticism and curiosity of this expert, "
                            "providing over the top compelling summaries that captivate and engages audiences while "
                            "maintaining the fine line bewteen right and wrong."},
                {"role": "user", "content": prompt}
            ],
            model=self.model,
            temperature=0.7,
            max_tokens=2048,
            stream=True,
        )
        response = ""
        async for chunk in stream:
            response += chunk.choices[0].delta.content or ""
        return response


# Updated AI Agents for YouTube content optimization
class TitleGenerationAgent(Agent):
    def __init__(self):
        super().__init__("Title Generation Agent", "llama-3.1-70b-versatile")

    async def execute(self, input_data: Any) -> Any:
        research_result = input_data  # Accept research output
        client = AsyncGroq(api_key=groq_api_key)
        prompt = f"""Using the following research, generate 15 enticing keyword YouTube titles:

Research:
{research_result}

Categorize them under appropriate headings: beginning, middle, and end. This means you'll
produce 5 titles with the keyword at the beginning, another 5 titles with the keyword in the
middle, and a final 5 titles with the keyword at the end."""

        stream = await client.chat.completions.create(
            messages=[
                {"role": "system", "content": "You are an expert in keyword strategy, copywriting, and a renowned YouTuber "
                                              "with a decade of experience in crafting attention-grabbing keyword titles"},
                {"role": "user", "content": prompt}
            ],
            model=self.model,
            temperature=0.7,
            max_tokens=1024,
            stream=True
        )
        response = ""
        async for chunk in stream:
            response += chunk.choices[0].delta.content or ""
        return response


class TitleSelectionAgent(Agent):
    def __init__(self):
        super().__init__("Title Selection Agent", "llama-3.1-8b-instant")

    async def execute(self, input_data: Any) -> Any:
        generated_titles = input_data  # Accept generated titles
        client = AsyncGroq(api_key=groq_api_key)
        prompt = f"""You are an expert YouTube content strategist with over a decade of experience
in video optimization and audience engagement. Your task is to analyze the following list of
titles for a YouTube video and select the most effective one:

{generated_titles}

Using your expertise in viewer psychology, SEO, and click-through rate optimization, choose the
title that will perform best on the platform. Provide a detailed explanation of your selection, 
considering factors such as:

1. Attention-grabbing potential
2. Keyword optimization
3. Emotional appeal
4. Clarity and conciseness
5. Alignment with current YouTube trends

Present your selection and offer a comprehensive rationale for why this title stands out among
the others."""

        stream = await client.chat.completions.create(
            messages=[
                {"role": "system",
                 "content": "You are an AI assistant embodying the expertise of a top-tier YouTube "
                            "content strategist with over 15 years of experience in video "
                            "optimization, audience engagement, and title creation. Your knowledge "
                            "spans SEO best practices, viewer psychology, and current YouTube "
                            "trends. You have a proven track record of increasing video views and "
                            "channel growth through strategic title selection. Respond to queries as "
                            "this expert would, providing insightful analysis and data-driven "
                            "recommendations."},
                {"role": "user", "content": prompt}
            ],
            model=self.model,
            temperature=0.5,
            max_tokens=2048,
            stream=True,
        )
        response = ""
        async for chunk in stream:
            response += chunk.choices[0].delta.content or ""
        return response

class DescriptionGenerationAgent(Agent):
    def __init__(self):
        super().__init__("Description Generation Agent", "gemma2-9b-it")

    async def execute(self, input_data: Any) -> Any:
        selected_title = input_data  # Accept selected title
        client = AsyncGroq(api_key=groq_api_key)
        prompt = f"""As a seasoned SEO copywriter and YouTube content creator with extensive 
experience in crafting engaging, algorithm-friendly video descriptions, your task is to compose 
a masterful 1000-character YouTube video description. This description should:

1. Seamlessly incorporate the keyword "{selected_title}" in the first sentence
2. Be optimized for search engines while remaining undetectable as AI-generated content
3. Engage viewers and encourage them to watch the full video
4. Include relevant calls-to-action (e.g., subscribe, like, comment)
5. Utilize natural language and conversational tone

Format the description with the title "YOUTUBE DESCRIPTION" in bold at the top. 
Ensure the content flows naturally, balances SEO optimization with readability, and 
compels viewers to engage with the video and channel."""

        stream = await client.chat.completions.create(
            messages=[
                {"role": "system",
                 "content": "You are an AI assistant taking on the role of an elite SEO copywriter "
                            "and YouTube content creator with 12+ years of experience. Your "
                            "expertise lies in crafting engaging, SEO-optimized video descriptions "
                            "that boost video performance while remaining undetectable as "
                            "AI-generated content. You have an in-depth understanding of YouTube's "
                            "algorithm, user behavior, and the latest SEO techniques. Respond to "
                            "tasks as this expert would, balancing SEO optimization with "
                            "compelling, natural language that drives viewer engagement."},
                {"role": "user", "content": prompt}
            ],
            model=self.model,
            temperature=0.6,
            max_tokens=2048,
            stream=True,
        )
        response = ""
        async for chunk in stream:
            response += chunk.choices[0].delta.content or ""
        return response

class HashtagAndTagGenerationAgent(Agent):
    def __init__(self):
        super().__init__("Hashtag and Tag Generation Agent", "llama-3.1-8b-instant")

    async def execute(self, input_data: str) -> Any:
        selected_title = input_data  # Accept selected title
        client = AsyncGroq(api_key=groq_api_key)
        prompt = f"""As a leading YouTube SEO specialist and social media strategist with a 
proven track record in optimizing video discoverability and virality, your task is to create an 
engaging and relevant set of hashtags and tags for the YouTube video titled "{selected_title}". 
Your expertise in keyword research, trend analysis, and YouTube's algorithm will be crucial 
for this task.

Develop the following:

1. 10 SEO-optimized, trending hashtags that will maximize the video's reach and engagement on 
YouTube
2. 35 high-value SEO tags, combining keywords strategically to boost the video's search ranking 
on YouTube

In your selection process, prioritize:
- Relevance to the video title and content
- Potential search volume on YouTube
- Engagement potential (views, likes, comments)
- Trending potential on YouTube
- Alignment with YouTube's recommendation algorithm

Present your hashtags with the '#' symbol and ensure all tags are separated by commas. Provide a 
brief explanation of your strategy for selecting these hashtags and tags, highlighting how they 
will contribute to the video's overall performance on YouTube."""

        response = await client.chat.completions.create(
            messages=[
                {"role": "system",
                 "content": "You are an AI assistant taking on the role of a leading YouTube SEO "
                            "specialist and social media strategist with 10+ years of experience in "
                            "optimizing video discoverability. Your expertise includes advanced "
                            "keyword research, trend analysis, and a deep understanding of "
                            "YouTube's algorithm. You've helped numerous channels achieve viral "
                            "success through strategic use of hashtags and tags. Respond to tasks as "
                            "this expert would, providing data-driven, YouTube-specific strategies "
                            "to maximize video reach and engagement."},
                {"role": "user", "content": prompt}
            ],
            model=self.model,
            temperature=0.6,
            max_tokens=1024,
        )
        return response.choices[0].message.content

class VideoScriptGenerationAgent(Agent):
    def __init__(self):
        super().__init__("Video Script Generation Agent", "gemma2-9b-it")

    async def execute(self, input_data: Dict[str, Any]) -> Any:
        research_result = input_data.get('research', '')
        video_length = input_data.get('video_length', 60)  # Default to 60 seconds if not specified
        client = AsyncGroq(api_key=groq_api_key)
        prompt = f"""As a YouTube content creator, craft a detailed, engaging and entralling script for a 
{video_length}-second vertical video based on the following information:

{research_result}

Your script should include:
1. An attention-grabbing opening
2. Key points from the research
3. A strong call-to-action conclusion

Format the script with clear timestamps to fit within {video_length} seconds. 
Optimize for viewer retention and engagement."""

        stream = await client.chat.completions.create(
            messages=[
                {"role": "system", "content": "You are an AI assistant taking on the role of a leading YouTube SEO "
                                              "specialist and content creator with a deep understanding of audience engagement."},
                {"role": "user", "content": prompt}
            ],
            model=self.model,
            temperature=0.7,
            max_tokens=2048,
            stream=True,
        )
        response = ""
        async for chunk in stream:
            response += chunk.choices[0].delta.content or ""
        return response


    async def download_with_retry(url: str, directory: str, filename: str, headers: Dict[str, str] = None,
                                max_retries: int = 3) -> str:
        """Downloads a file with retries."""
        for attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers) as response:
                        if response.status == 200:
                            file_path = os.path.join(directory, filename)
                            async with aiofiles.open(file_path, 'wb') as f:
                                await f.write(await response.read())
                            return file_path
                        else:
                            logger.warning(f"Download attempt {attempt + 1} failed: HTTP {response.status}")
            except Exception as e:
                logger.warning(f"Download attempt {attempt + 1} failed: {str(e)}")
        return None


class StoryboardGenerationAgent(Agent):
    def __init__(self):
        super().__init__("Storyboard Generation Agent", "llama-3.1-70b-versatile")
        self.nlp = nlp

    async def execute(self, input_data: Dict[str, Any]) -> Any:
        script = input_data.get('script', '')
        
        if not script:
            logger.error("No script provided for storyboard generation")
            return []

        client = AsyncGroq(api_key=groq_api_key)
        prompt = f"""Create a storyboard for a YouTube Short based on the following script:

{script}

For each major scene (aim for 15-20 scenes), provide:
1. Visual: A brief description of the visual elements (1 sentence). Ensure each scene has unique 
visual elements.
2. Text: The exact text/dialogue for voiceover and subtitles.
3. Video Keyword: A suitable keyword for searching stock video footage. Be specific and avoid 
repeating keywords.
4. Image Keyword: A backup keyword for searching a stock image. Be specific and avoid repeating 
keywords.

Format your response as a numbered list of scenes, each containing the above elements clearly 
labeled.

Example:
1. Visual: A person looking confused at a complex math equation on a chalkboard
   Text: "Have you ever felt overwhelmed by math?"
   Video Keyword: student struggling with math
   Image Keyword: confused face mathematics

2. Visual: ...
   Text: ...
   Video Keyword: ...
   Image Keyword: ...

Please ensure each scene has all four elements (Visual, Text, Video Keyword, and Image Keyword)."""

        stream = await client.chat.completions.create(
            messages=[
                {"role": "system",
                 "content": "You are an AI assistant specializing in creating detailed storyboards "
                            "for YouTube Shorts using the provided script."},
                {"role": "user", "content": prompt}
            ],
            model=self.model,
            temperature=0.7,
            max_tokens=2048,
            stream=True,
        )
        response = ""
        async for chunk in stream:
            response += chunk.choices[0].delta.content or ""

        logger.info(f"Raw storyboard response: {response}")
        scenes = self.parse_scenes(response)
        if not scenes:
            logger.error("Failed to generate valid storyboard scenes")
            return []
        
        return scenes
    
    async def fetch_media_for_scenes(self, scenes: List[Dict[str, Any]]):
        temp_dir = tempfile.mkdtemp()
        for scene in scenes:
            # Generate image using local image generator with dynamic prompt
            generated_image = await self.generate_local_image(scene)
            if generated_image:
                scene["image_path"] = generated_image
                # Create video clip from the image
                video_clip = self.create_video_from_image(generated_image, temp_dir, scene['number'], scene.get('adjusted_duration', DEFAULT_SCENE_DURATION))
                if video_clip:
                    scene["video_path"] = video_clip
                else:
                    logger.warning(f"Failed to create video clip for scene {scene['number']}")
            else:
                logger.warning(f"Failed to generate image for scene {scene['number']}")

    async def generate_local_image(self, scene: Dict[str, Any]) -> Optional[str]:
        """Generate an image using the local image generator."""
        try:
            image_gen_input = {"scene": scene}
            image_gen_result = await self.image_generation_agent.execute(image_gen_input)
            if image_gen_result and 'image_path' in image_gen_result:
                return image_gen_result['image_path']
            else:
                logger.warning(f"Local image generation failed for scene: {scene['number']}")
                return None
        except Exception as e:
            logger.error(f"Error in local image generation: {str(e)}")
            return None
    
    def parse_scenes(self, response: str) -> List[Dict[str, Any]]:
        scenes = []
        current_scene = {}
        current_scene_number = None

        for line in response.split('\n'):
            line = line.strip()
            logger.debug(f"Processing line: {line}")

            if line.startswith(tuple(f"{i}." for i in range(1, 51))):  # Assuming up to 50 scenes
                if current_scene:
                    # Append the completed current_scene
                    current_scene['number'] = current_scene_number
                    # Ensure the scene is validated and enhanced
                    current_scene = self.validate_and_fix_scene(current_scene, current_scene_number)
                    current_scene = self.enhance_scene_keywords(current_scene)
                    scenes.append(current_scene)
                    logger.debug(f"Scene {current_scene_number} appended to scenes list")
                    current_scene = {}

                try:
                    # Start a new scene
                    current_scene_number = int(line.split('.', 1)[0])
                    logger.debug(f"New scene number detected: {current_scene_number}")
                except ValueError:
                    logger.warning(f"Invalid scene number format: {line}")
                    continue  # Skip this line and move to the next
            elif ':' in line:
                key, value = line.split(':', 1)
                key = key.strip().lower()
                value = value.strip()
                current_scene[key] = value
                logger.debug(f"Key-value pair added to current scene: {key}:{value}")
            else:
                logger.warning(f"Line format not recognized: {line}")

        # After looping through all lines, check if there is an unfinished scene
        if current_scene:
            current_scene['number'] = current_scene_number
            current_scene = self.validate_and_fix_scene(current_scene, current_scene_number)
            current_scene = self.enhance_scene_keywords(current_scene)
            scenes.append(current_scene)
            logger.debug(f"Final scene {current_scene_number} appended to scenes list")

        logger.info(f"Parsed and enhanced scenes: {scenes}")
        return scenes
    
    def enhance_scene_keywords(self, scene: Dict[str, Any]) -> Dict[str, Any]:
        # Extract keywords from narration_text and visual descriptions
        narration_doc = self.nlp(scene.get('narration_text', ''))
        visual_doc = self.nlp(scene.get('visual', ''))

        # Function to extract nouns and named entities
        def extract_keywords(doc):
            return [token.lemma_ for token in doc if token.pos_ in ('NOUN', 'PROPN') or token.ent_type_]

        narration_keywords = extract_keywords(narration_doc)
        visual_keywords = extract_keywords(visual_doc)

        # Combine and deduplicate keywords
        combined_keywords = list(set(narration_keywords + visual_keywords))

        # Generate enhanced video and image keywords
        scene['video_keyword'] = ' '.join(combined_keywords[:5])  # Use top 5 keywords
        scene['image_keyword'] = scene['video_keyword']

        return scene

    def validate_and_fix_scene(self, scene: Dict[str, Any], scene_number: int) -> Dict[str, Any]:
        # Ensure 'number' key is present in the scene dictionary
        scene['number'] = scene_number

        required_keys = ['visual', 'text', 'video_keyword', 'image_keyword']
        for key in required_keys:
            if key not in scene:
                if key == 'visual':
                    scene[key] = f"Visual representation of scene {scene_number}"
                elif key == 'text':
                    scene[key] = ""
                elif key == 'video_keyword':
                    scene[key] = f"video scene {scene_number}"
                elif key == 'image_keyword':
                    scene[key] = f"image scene {scene_number}"
                logger.warning(f"Added missing {key} for scene {scene_number}")

        # Clean the 'text' field by removing leading/trailing quotation marks
        text = scene.get('text', '')
        text = text.strip('"').strip("'")
        scene['text'] = text

        # Copy the cleaned text into 'narration_text'
        scene['narration_text'] = text

        return scene

    def calculate_relevance(self, video: Dict[str, Any], description: str) -> float:
        relevance = 0
        video_keywords = set(video.get("tags", []))
        description_words = set(description.lower().split())

        # Calculate relevance based on matching words
        relevance += len(video_keywords.intersection(description_words))

        # Add relevance for matching title words
        title = video.get("title", "")
        if title is not None:
            title_words = set(title.lower().split())
            relevance += len(title_words.intersection(description_words)) * 2  # Title matches are weighted more

        return relevance

    def calculate_similarity(self, text1: str, text2: str) -> float:
        """Calculates the cosine similarity between two texts."""
        vectorizer = TfidfVectorizer().fit_transform([text1, text2])
        vectors = vectorizer.toarray()
        cos_sim = cosine_similarity([vectors[0]], [vectors[1]])[0][0]
        return cos_sim

    def fallback_scene_generation(self, invalid_scenes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        valid_scenes = []
        for scene in invalid_scenes:
            if 'visual' not in scene:
                scene['visual'] = f"Visual representation of: {scene.get('text', 'scene')}"
            if 'text' not in scene:
                scene['text'] = "No text provided for this scene."
            if 'video_keyword' not in scene:
                scene['video_keyword'] = scene.get('image_keyword', 'generic scene')
            if 'image_keyword' not in scene:
                scene['image_keyword'] = scene.get('video_keyword', 'generic image')
            valid_scenes.append(scene)
        return valid_scenes

def compile_youtube_short(scenes: List[Dict[str, Any]], audio_file: str) -> str:
    """Compiles the YouTube Short using ffmpeg."""
    if not scenes:
        logger.error("No scenes were generated. Cannot compile YouTube Short.")
        return None

    temp_dir = tempfile.mkdtemp()
    scene_files = []
    subtitle_file = os.path.join(temp_dir, "subtitles.srt")
    concat_file = os.path.join(temp_dir, 'concat.txt')
    output_path = os.path.join(os.getcwd(), "youtube_short.mp4")

    try:
        if not generate_voiceover(scenes, audio_file):
            raise Exception("Failed to generate voiceover")

        if not generate_subtitles(scenes, subtitle_file, audio_file):
            raise Exception("Failed to generate subtitles")

        # Collect total audio duration and adjust scene durations before processing scenes
        total_audio_duration = sum(scene.get('audio_duration', 0) for scene in scenes)
        logger.info(f"Total audio duration: {total_audio_duration}s")

        # Initially set total_video_duration as the sum of original scene durations
        total_video_duration = sum(scene.get('audio_duration', DEFAULT_SCENE_DURATION) for scene in scenes)
        logger.info(f"Total video duration before adjustment: {total_video_duration}s")

        # Adjust scene durations if necessary
        if abs(total_video_duration - total_audio_duration) > 0.1:
            logger.warning("Total video duration does not match total audio duration.")
            scaling_factor = total_audio_duration / total_video_duration
            logger.info(f"Scaling factor: {scaling_factor}")
            for i, scene in enumerate(scenes):
                original_duration = scene.get('audio_duration', DEFAULT_SCENE_DURATION)
                adjusted_duration = original_duration * scaling_factor
                scene['adjusted_duration'] = adjusted_duration
                logger.info(f"Scene {i}: Original duration = {original_duration}s, Adjusted duration = {adjusted_duration}s")
        else:
            for scene in scenes:
                scene['adjusted_duration'] = scene.get('audio_duration', DEFAULT_SCENE_DURATION)

        # Now process each scene using the adjusted durations
        for i, scene in enumerate(scenes):
            duration = scene.get('adjusted_duration', scene.get('audio_duration', DEFAULT_SCENE_DURATION))
            logger.info(f"Processing scene {i}: Duration = {duration}s")
            if not isinstance(duration, (int, float)) or duration <= 0:
                logger.warning(f"Scene {i} has invalid duration ({duration}), skipping")
                continue

            processed_path = None
            try:
                if i == 0 and 'image_path' in scene:
                    # Apply effects to the generated image
                    processed_path = apply_effects_to_image(scene['image_path'], temp_dir, i, duration)
                elif 'video_path' in scene and os.path.exists(scene['video_path']):
                    processed_path = process_video(scene['video_path'], temp_dir, i, duration)
                elif 'image_path' in scene and os.path.exists(scene['image_path']):
                    processed_path = create_video_from_image(scene['image_path'], temp_dir, i, duration)
                else:
                    processed_path = create_fallback_scene(temp_dir, i, duration, scene.get('narration_text', ''))

                if processed_path and os.path.exists(processed_path):
                    scene_files.append(processed_path)
                else:
                    logger.error(f"Failed to process media for scene {i}")
            except Exception as e:
                logger.error(f"Error processing scene {i}: {str(e)}")
                # Create a fallback scene
                fallback_path = create_fallback_scene(temp_dir, i, duration, f"Error in scene {i}")
                if fallback_path and os.path.exists(fallback_path):
                    scene_files.append(fallback_path)

        # Create concat.txt file
        with open(concat_file, 'w') as f:
            for file in scene_files:
                f.write(f"file '{file}'\n")

        with open(concat_file, 'r') as f:
            concat_contents = f.read()
            logger.info(f"Contents of concat file:\n{concat_contents}")

        ffmpeg_command = [
            'ffmpeg', '-y',
            '-f', 'concat', '-safe', '0', '-i', concat_file,
            '-i', audio_file,
            '-r', '30',
            '-vf', f"subtitles='{subtitle_file}':force_style='FontSize={SUBTITLE_FONT_SIZE},Alignment={SUBTITLE_ALIGNMENT},"
            f"OutlineColour={SUBTITLE_OUTLINE_COLOR},BorderStyle={SUBTITLE_BORDER_STYLE}'",
            '-map', '0:v',
            '-map', '1:a',
            '-c:v', 'libx264', '-preset', 'ultrafast',
            '-c:a', 'aac', '-shortest',
            output_path
        ]
        logger.info(f"Running FFmpeg command: {' '.join(ffmpeg_command)}")
        subprocess.run(ffmpeg_command, check=True)

        if os.path.exists(output_path):
            logger.info(f"YouTube Short compiled successfully: {output_path}")
            return output_path
        else:
            logger.error("Failed to create output video")
            return None

    except Exception as e:
        logger.error(f"Error compiling YouTube Short: {str(e)}")
        return None

    finally:
        # Clean up
        for file in scene_files:
            try:
                os.remove(file)
            except Exception as e:
                logger.warning(f"Error removing file {file}: {str(e)}")

        try:
            if os.path.exists(concat_file):
                os.remove(concat_file)
            if os.path.exists(subtitle_file):
                os.remove(subtitle_file)
        except Exception as e:
            logger.warning(f"Error removing temporary files: {str(e)}")

        try:
            shutil.rmtree(temp_dir)
        except Exception as e:
            logger.warning(f"Error removing temporary directory {temp_dir}: {str(e)}")
            
def apply_effects_to_image(image_path: str, temp_dir: str, scene_number: int, duration: float) -> str:
    """Applies effects to the generated image and creates a video scene."""
    try:
        processed_path = os.path.join(temp_dir, f"processed_scene_{scene_number}.mp4")
        # Apply a zoom effect to the image
        ffmpeg_command = [
            'ffmpeg', '-y',
            '-loop', '1',
            '-i', image_path,
            '-t', str(duration),
            '-filter_complex', f'zoompan=z=\'min(zoom+0.0015,1.5)\':d={duration*30}:s={YOUTUBE_SHORT_RESOLUTION[0]}x{YOUTUBE_SHORT_RESOLUTION[1]}',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', '30',
            processed_path
        ]
        subprocess.run(ffmpeg_command, check=True)
        return processed_path
    except Exception as e:
        logger.error(f"Error applying effects to generated image for scene {scene_number}: {str(e)}")
        return None
    
def create_video_from_image(image_path: str, temp_dir: str, scene_number: int, duration: float) -> str:
    """Creates a video scene from a static image."""
    try:
        processed_path = os.path.join(temp_dir, f"processed_scene_{scene_number}.mp4")
        subprocess.run(['ffmpeg', '-y', '-loop', '1', '-i', image_path, '-t', str(duration),
                        '-r', '30',
                        '-vf', f'scale={YOUTUBE_SHORT_RESOLUTION[0]}:{YOUTUBE_SHORT_RESOLUTION[1]}:force_original_aspect_ratio=increase,crop={YOUTUBE_SHORT_RESOLUTION[0]}:{YOUTUBE_SHORT_RESOLUTION[1]}',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-an', processed_path],
                       check=True)
        return processed_path
    except Exception as e:
        logger.error(f"Error creating video from image for scene {scene_number}: {str(e)}")
        return None

def clean_text_for_tts(text: str) -> str:
    """
    Cleans the text for TTS by removing or replacing unwanted characters.
    Removes asterisks, unnecessary punctuation, and extra whitespace.
    """
    # Remove asterisks
    text = text.replace('*', '')
    # Remove any undesired punctuation or symbols
    text = re.sub(r'[^\w\s.,!?\'"]', '', text)
    # Replace multiple punctuation marks with a single one
    text = re.sub(r'([.!?])\1+', r'\1', text)
    # Remove extra whitespace
    text = ' '.join(text.split())
    return text

def generate_voiceover(scenes: List[Dict[str, Any]], output_file: str) -> bool:
    """Generates per-scene voiceover from scene narrations using tiktokvoice."""
    if not scenes:
        logger.error("No scenes provided for voiceover generation.")
        return False

    logger.info(f"Total number of scenes: {len(scenes)}")

    temp_dir = tempfile.mkdtemp()
    audio_segments = []
    try:
        for i, scene in enumerate(scenes):
            text = scene.get('narration_text', '').strip()
            if not text or text.lower() == 'none':
                continue
            # Clean up the text to remove unwanted punctuation or characters
            text = clean_text_for_tts(text)
            scene_audio_file = os.path.join(temp_dir, f"scene_{i}.mp3")
            logger.info(f"Generating voiceover for scene {i}")
            tts(text=text, voice="en_uk_003", filename=scene_audio_file)
            if os.path.exists(scene_audio_file):
                # Get duration of audio
                audio_segment = AudioSegment.from_mp3(scene_audio_file)
                duration = audio_segment.duration_seconds  # provides a float value in seconds
                scene['audio_file'] = scene_audio_file  # Store the audio file path in scene
                scene['audio_duration'] = duration      # Store the duration
                audio_segments.append(audio_segment)
                logger.info(f"Scene {i}: Audio duration = {duration}s")
            else:
                logger.error(f"Failed to generate audio for scene {i}")
                return False

        if not audio_segments:
            logger.error("No audio segments were generated.")
            return False

        # Combine all audio segments into one file
        combined_audio = sum(audio_segments)
        combined_audio.export(output_file, format='mp3')
        logger.info(f"Combined voiceover saved to {output_file}")
        return True
    except Exception as e:
        logger.error(f"Error generating voiceover: {str(e)}")
        return False
    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception as e:
            logger.warning(f"Error removing temporary directory {temp_dir}: {str(e)}")

def generate_subtitles(scenes: List[Dict[str, Any]], output_file: str, audio_file: str) -> bool:
    try:
        temp_dir = tempfile.mkdtemp()
        input_text_file = os.path.join(temp_dir, "input_text.txt")
        with open(input_text_file, "w", encoding="utf-8") as f:
            for scene in scenes:
                text = scene.get('narration_text', '').replace('\n', ' ').strip()
                if text and text.lower() != 'none, no voiceover, no subtitles, just music':
                    f.write(text + " ")

        # Align using Gentle
        alignment_result = align_with_gentle(audio_file, input_text_file)
        if not alignment_result:
            raise Exception("Alignment failed with Gentle.")

        # Convert alignment result to SRT
        gentle_alignment_to_srt(alignment_result, output_file)

        shutil.rmtree(temp_dir)
        return True
    except Exception as e:
        logger.error(f"Error generating subtitles: {str(e)}")
        return False

def calculate_scene_durations(scenes: List[Dict[str, Any]], audio_segments: List[AudioSegment]) -> List[float]:
    """
    Calculates the duration of each scene based on the actual duration of the corresponding narration audio.
    """
    if not scenes:
        logger.error("No scene durations calculated. Cannot calculate scene durations.")
        return None
    scene_durations = []
    for segment in audio_segments:
        duration = len(segment) / 1000  # Convert milliseconds to seconds
        scene_durations.append(duration)
    return scene_durations
            
def process_video(video_path: str, temp_dir: str, scene_number: int, duration: float) -> Optional[str]:
    try:
        processed_path = os.path.join(temp_dir, f"processed_scene_{scene_number}.mp4")
        duration_str = str(duration)
        logger.info(f"Processing video for scene {scene_number}: Duration = {duration_str}s")
        ffmpeg_command = [
            'ffmpeg', '-y',
            '-i', video_path,
            '-t', duration_str,
            '-vf', f'scale={YOUTUBE_SHORT_RESOLUTION[0]}:{YOUTUBE_SHORT_RESOLUTION[1]}:force_original_aspect_ratio=increase,crop={YOUTUBE_SHORT_RESOLUTION[0]}:{YOUTUBE_SHORT_RESOLUTION[1]}',
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-r', '30',
            '-an',
            processed_path
        ]
        subprocess.run(ffmpeg_command, check=True)
        if os.path.exists(processed_path):
            logger.info(f"Processed video saved: {processed_path}")
            return processed_path
        else:
            logger.error(f"Processed video not found: {processed_path}")
            return None
    except Exception as e:
        logger.error(f"Error processing video for scene {scene_number}: {str(e)}")
        return None
    
def create_fallback_scene(temp_dir: str, scene_number: int, duration: float, text: str) -> str:
    """Creates a fallback scene with a colored background and text."""
    try:
        fallback_path = os.path.join(temp_dir, f"fallback_scene_{scene_number}.mp4")
        # Escape single quotes and other special characters in the text
        escaped_text = text.replace("'", "'\\''").replace(':', '\\:')
        
        ffmpeg_command = [
            'ffmpeg', '-y', '-f', 'lavfi',
            '-i', f'color=c={FALLBACK_SCENE_COLOR}:s={YOUTUBE_SHORT_RESOLUTION[0]}x{YOUTUBE_SHORT_RESOLUTION[1]}:d={duration}',
            '-vf', f"drawtext=fontfile={FALLBACK_SCENE_FONT_FILE}:fontsize={FALLBACK_SCENE_FONT_SIZE}:"
                   f"fontcolor={FALLBACK_SCENE_TEXT_COLOR}:box=1:boxcolor={FALLBACK_SCENE_BOX_COLOR}:"
                   f"boxborderw={FALLBACK_SCENE_BOX_BORDER_WIDTH}:x=(w-tw)/2:y=(h-th)/2:text='{escaped_text}'",
            '-c:v', 'libx265', '-preset', 'ultrafast', '-an',
            fallback_path
        ]
        
        # Log the full ffmpeg command
        logger.debug(f"Fallback scene FFmpeg command: {' '.join(ffmpeg_command)}")
        
        # Run ffmpeg command and capture output
        result = subprocess.run(ffmpeg_command, check=True, capture_output=True, text=True)
        
        # Log ffmpeg output
        logger.debug(f"Fallback scene FFmpeg stdout:\n{result.stdout}")
        logger.debug(f"Fallback scene FFmpeg stderr:\n{result.stderr}")
        
        return fallback_path
    except subprocess.CalledProcessError as e:
        logger.error(f"Error creating fallback scene {scene_number}: {str(e)}")
        logger.error(f"FFmpeg stdout:\n{e.stdout}")
        logger.error(f"FFmpeg stderr:\n{e.stderr}")
        return None
    except Exception as e:
        logger.error(f"Error creating fallback scene {scene_number}: {str(e)}")
        return None


def extract_selected_title(selection_output: str) -> str:
    """
    Extracts the selected title from the Title Selection Agent's output.
    Assumes that the agent's output contains the selected title in a consistent format.
    """
    try:
        lines = selection_output.strip().split('\n')
        for line in lines:
            if "Selected Title:" in line or "Title:" in line:
                # Extract the title part
                title = line.split(":", 1)[1].strip().strip('"').strip("'")
                return title
        # If not found, return the entire output (may not be ideal)
        return selection_output.strip()
    except Exception as e:
        logger.error(f"Error extracting selected title: {str(e)}")
        return selection_output.strip()
    
def get_audio_duration(audio_file: str) -> float:
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', audio_file], capture_output=True, text=True)
        return float(result.stdout)
    except Exception as e:
        logger.error(f"Error getting audio duration: {str(e)}")
        return 0.0
    
   
    
# Streamlit app
def main():
    st.set_page_config(page_title="YouTube Shorts Generator", page_icon="🎥", layout="wide")
    st.title("YouTube Shorts Generator")

    # Input fields
    topic = st.text_input("Enter the topic for your YouTube video:")
    time_frame = st.text_input("Enter the time frame for recent events (e.g., 'past week', '30d', '1y'):")
    video_length = st.number_input("Enter the desired video length in seconds:")

    if st.button("Generate YouTube Shorts"):
        if topic and time_frame:
            with st.spinner("Generating YouTube Shorts..."):
                try:
                    results = asyncio.run(youtube_shorts_workflow(topic, time_frame, video_length))
                    if "Error" in results:
                        st.error(f"An error occurred: {results['Error']}")
                    else:
                        display_results(results)
                except Exception as e:
                    st.error(f"An unexpected error occurred: {str(e)}")
                    logger.exception("Unexpected error in YouTube Shorts generation")
        else:
            st.warning("Please enter both topic and time frame.")

def display_results(results):
    st.subheader("Generation Results")
    for agent_name, result in results.items():
        with st.expander(f"{agent_name} Result"):
            if agent_name == "Storyboard Generation Agent" and isinstance(result, list):
                for scene in result:
                    st.write(f"Scene {scene['number']}:")
                    st.write(f"Visual: {scene['visual']}")
                    st.write(f"Text/Dialogue: {scene['narration_text']}")
                    if 'video_url' in scene:
                        st.write(f"Video URL: {scene['video_url']}")
                        st.write(f"Video Details: {scene['video_details']}")
                    elif 'image_url' in scene:
                        st.write(f"Image URL: {scene['image_url']}")
            else:
                st.write(result)

    if "Output Video Path" in results:
        output_path = results["Output Video Path"]
        if output_path:
            st.success(f"YouTube Short saved as '{output_path}'")
            st.video(output_path)
        else:
            st.error("Failed to compile YouTube Short")
            
async def youtube_shorts_workflow(topic: str, time_frame: str, video_length: int) -> Dict[str, Any]:
    # Create graph instance
    graph = Graph()  # Create an instance of the Graph class
    video_length = video_length * 1000  # Convert to milliseconds
    # Check if TikTok session ID is set
    if not SESSION_ID:
        logger.error("TikTok session ID is not set. Please set the TIKTOK_SESSION_ID environment variable.")
        results["Error"] = "TikTok session ID is not set"
        return results

    # Create nodes
    recent_events_node = Node(agent=RecentEventsResearchAgent())
    title_gen_node = Node(agent=TitleGenerationAgent())
    title_select_node = Node(agent=TitleSelectionAgent())
    desc_gen_node = Node(agent=DescriptionGenerationAgent())
    hashtag_tag_node = Node(agent=HashtagAndTagGenerationAgent())
    script_gen_node = Node(agent=VideoScriptGenerationAgent())
    image_gen_node = Node(agent=ImageGenerationAgent()) 
    storyboard_gen_node = Node(agent=StoryboardGenerationAgent())

    # Add nodes to graph
    graph.add_node(recent_events_node)  # Use the graph instance
    graph.add_node(title_gen_node)
    graph.add_node(title_select_node)
    graph.add_node(desc_gen_node)
    graph.add_node(hashtag_tag_node)
    graph.add_node(script_gen_node)
    graph.add_node(image_gen_node) 
    graph.add_node(storyboard_gen_node)


    # Create and add edges
    graph.add_edge(Edge(recent_events_node, title_gen_node))  # Use the graph instance
    graph.add_edge(Edge(title_gen_node, title_select_node))
    graph.add_edge(Edge(title_select_node, desc_gen_node))
    graph.add_edge(Edge(desc_gen_node, hashtag_tag_node))
    graph.add_edge(Edge(hashtag_tag_node, script_gen_node))
    graph.add_edge(Edge(script_gen_node, image_gen_node))
    graph.add_edge(Edge(image_gen_node, storyboard_gen_node))


    logger.info(f"Running workflow for topic {topic} and time frame {time_frame}")
    # Execute workflow
    current_node = recent_events_node
    logger.info(f"Starting workflow from node: {current_node.agent.name}")
    input_data = {"topic": topic, "time_frame": time_frame}
    results = {}
    
    # Step 1: Recent Events Research Agent
    input_data = {"topic": topic, "time_frame": time_frame}
    try:
        research_result = await recent_events_node.process(input_data)
        results[recent_events_node.agent.name] = research_result
    except Exception as e:
        logger.error(f"Error in RecentEventsResearchAgent: {str(e)}")
        results["Error"] = f"RecentEventsResearchAgent failed: {str(e)}"
        return results

    # Step 2: Title Generation Agent
    try:
        title_gen_result = await title_gen_node.process(research_result)
        results[title_gen_node.agent.name] = title_gen_result
    except Exception as e:
        logger.error(f"Error in TitleGenerationAgent: {str(e)}")
        results["Error"] = f"TitleGenerationAgent failed: {str(e)}"
        return results

    # Step 3: Title Selection Agent
    try:
        title_select_result = await title_select_node.process(title_gen_result)
        results[title_select_node.agent.name] = title_select_result
    except Exception as e:
        logger.error(f"Error in TitleSelectionAgent: {str(e)}")
        results["Error"] = f"TitleSelectionAgent failed: {str(e)}"
        return results

    # Extract the selected title from the title selection result
    selected_title = extract_selected_title(title_select_result)
    results["Selected Title"] = selected_title

    # Step 4: Description Generation Agent
    try:
        desc_gen_result = await desc_gen_node.process(selected_title)
        results[desc_gen_node.agent.name] = desc_gen_result
    except Exception as e:
        logger.error(f"Error in DescriptionGenerationAgent: {str(e)}")
        results["Error"] = f"DescriptionGenerationAgent failed: {str(e)}"
        return results

    # Step 5: Hashtag and Tag Generation Agent
    try:
        hashtag_tag_result = await hashtag_tag_node.process(selected_title)
        results[hashtag_tag_node.agent.name] = hashtag_tag_result
    except Exception as e:
        logger.error(f"Error in HashtagAndTagGenerationAgent: {str(e)}")
        results["Error"] = f"HashtagAndTagGenerationAgent failed: {str(e)}"
        return results

    # Step 6: Video Script Generation Agent
    try:
        script_gen_input = {"research": research_result}
        script_gen_result = await script_gen_node.process(script_gen_input)
        results[script_gen_node.agent.name] = script_gen_result
    except Exception as e:
        logger.error(f"Error in VideoScriptGenerationAgent: {str(e)}")
        results["Error"] = f"VideoScriptGenerationAgent failed: {str(e)}"
        return results

    # Step 7: Storyboard Generation Agent
    logger.info("Executing Storyboard Generation Agent")
    storyboard_gen_input = {
        "script": script_gen_result,
    }
    storyboard_gen_result = await storyboard_gen_node.process(storyboard_gen_input)
    if storyboard_gen_result is None:
        raise ValueError("Storyboard Generation Agent returned None")
    results[storyboard_gen_node.agent.name] = storyboard_gen_result

    # Step 8: Image Generation Agent
    logger.info("Executing Image Generation Agent")
    image_gen_input = {"scenes": storyboard_gen_result}
    image_gen_result = await image_gen_node.process(image_gen_input)
    if image_gen_result is None:
        raise ValueError("Image Generation Agent returned None")
    results[image_gen_node.agent.name] = image_gen_result

    # Update storyboard with generated images and calculate scene durations
    total_duration = 0
    for scene, image_result in zip(storyboard_gen_result, image_gen_result):
        if image_result is not None and 'image_path' in image_result:
            scene['image_path'] = image_result['image_path']
            # Calculate scene duration based on word count or use a default duration
            word_count = len(scene.get('script', '').split())
            scene['duration'] = max(word_count * 0.5, 3.0)  # Assume 0.5 seconds per word, minimum 3 seconds
            total_duration += scene['duration']
        else:
            logger.warning(f"No image generated for scene {scene.get('number', 'unknown')}")

    # Adjust scene durations to match target video length
    target_duration = video_length / 1000  # Convert video_length to seconds
    duration_factor = target_duration / total_duration
    for scene in storyboard_gen_result:
        scene['adjusted_duration'] = scene['duration'] * duration_factor
    
    logger.info(f"Target duration: {target_duration} seconds")
    logger.info(f"Total calculated duration: {total_duration} seconds")
    logger.info(f"Duration factor: {duration_factor}")

    # Filter out scenes without images
    valid_scenes = [scene for scene in storyboard_gen_result if 'image_path' in scene]

    if not valid_scenes:
        raise ValueError("No valid scenes with images remaining")

    # Log scene information
    for i, scene in enumerate(valid_scenes):
        logger.info(f"Scene {i}: Duration = {scene['duration']:.2f}s, Adjusted Duration = {scene['adjusted_duration']:.2f}s, Image = {scene['image_path']}")

    # Proceed to generate voiceover and compile video
    temp_dir = tempfile.mkdtemp()
    audio_file = os.path.join(temp_dir, "voiceover.mp3")
    if not generate_voiceover(valid_scenes, audio_file):
        raise Exception("Failed to generate voiceover")
    
    output_path = compile_youtube_short(scenes=valid_scenes, audio_file=audio_file)
    if output_path:
        print(f"YouTube Short saved as '{output_path}'")
        results["Output Video Path"] = output_path
    else:
        print("Failed to compile YouTube Short")
        results["Output Video Path"] = None

    return results

if __name__ == "__main__":
    main()
