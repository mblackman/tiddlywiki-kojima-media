#!/usr/bin/env python3
"""
Hideo Kojima Movie Recommendation Sync, AI Enthusiasm Scoring & RSS Scraper Service
Integrates with mblackman/tiddlywiki-media-importer (TMDB base tiddler pipeline).

Features:
- Aggregates Hideo Kojima movie reviews & recommendations across multiple sources:
  1. GQ Top 4 & Rotten Tomatoes Five Favorite Films
  2. "My Favorite Films" (Metal Gear Solid Naked / Junker HQ essays)
  3. Criterion Collection Closet Picks
  4. Annual "Top Movies of the Year" lists (X / Instagram)
  5. X (@HIDEO_KOJIMA_EN) movie reviews (via RSS/Nitter/Letterboxd feeds + curated seed)
- Persists recommendations, resolved TMDB IDs, and LLM sentiment scores in SQLite
  (`/data/kojima_reviews.db` or `KOJIMA_DB_PATH`) so repeat syncs are fast and deterministic.
- Background RSS/Atom scraper daemon (`KOJIMA_RSS_URLS`, `SCRAPE_INTERVAL_SECONDS`)
  that extracts movie titles from new `@HIDEO_KOJIMA_EN` posts, resolves `tmdb_id`,
  and scores enthusiasm automatically.
- Exposes a CORS-enabled HTTP JSON endpoint (`GET /api/kojima-recommendations`,
  `POST /api/scrape`, `POST /api/ingest-post`, `GET /health`) for one-click syncing
  directly from TiddlyWiki in the browser.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Configuration (via Environment Variables)
# ---------------------------------------------------------------------------
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "heuristic").lower()  # "openai", "gemini", "ollama", or "heuristic"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
PORT = int(os.getenv("PORT", "8765"))

DEFAULT_DB_DIR = "/data" if os.path.isdir("/data") and os.access("/data", os.W_OK) else os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
KOJIMA_DB_PATH = os.getenv("KOJIMA_DB_PATH", os.path.join(DEFAULT_DB_DIR, "kojima_reviews.db"))
DEFAULT_KOJIMA_RSS_URLS = [
    "https://letterboxd.com/kojima_hideo/rss/",
]
DEFAULT_KOJIMA_NEWS_RSS_URLS = [
    "https://news.google.com/rss/search?q=%22Hideo+Kojima%22+(movie+OR+film+OR+review+OR+watched)+when:1y&hl=en-US&gl=US&ceid=US:en",
    "https://www.bing.com/news/search?q=%22Hideo+Kojima%22+review+movie&format=rss",
]
_env_rss_urls = [u.strip() for u in os.getenv("KOJIMA_RSS_URLS", "").split(",") if u.strip()]
KOJIMA_RSS_URLS = _env_rss_urls if _env_rss_urls else list(DEFAULT_KOJIMA_RSS_URLS)
KOJIMA_DISCOVER_X_REVIEWS = os.getenv("KOJIMA_DISCOVER_X_REVIEWS", "1") not in ("0", "false", "no")
SCRAPE_INTERVAL_SECONDS = int(os.getenv("SCRAPE_INTERVAL_SECONDS", "0"))

# ---------------------------------------------------------------------------
# Source Prestige Weights (0 - 25 pts max) & Default Recommendation Dates
# ---------------------------------------------------------------------------
SOURCE_PRESTIGE_WEIGHTS: Dict[str, int] = {
    "gq_top_4": 25,             # GQ All-Time Top 4
    "rt_top_5": 25,             # Rotten Tomatoes Five Favorite Films
    "mgs_dna_core": 23,         # Junker HQ Core 7 films that shaped Metal Gear Solid
    "junkerhq_top_30": 21,      # Junker HQ All-Time Top 30 Personal Favorites
    "criterion_closet": 20,     # Criterion Collection Closet Picks (9 hand-picked classics)
    "annual_top_list": 19,      # Year-End Top Movies List (top ~4% of 250+ watched per year)
    "junkerhq_runner_up": 15,   # Junker HQ Runners-Up / MGS Influences
    "x_review": 10,             # Standard X (@HIDEO_KOJIMA_EN) review post (boosted to 13 on raves)
}

SOURCE_DEFAULT_DATES: Dict[str, str] = {
    "gq_top_4": "2025-05-14",
    "rt_top_5": "2019-11-08",
    "mgs_dna_core": "2008-06-12",
    "junkerhq_top_30": "2008-06-12",
    "criterion_closet": "2024-11-12",
    "annual_top_list": "2026-01-06",
    "junkerhq_runner_up": "2008-06-12",
    "x_review": "2025-12-29",
}

MOVIE_RECOMMENDED_DATES: Dict[str, str] = {
    "Avatar: Fire and Ash": "2025-12-29",
    "Conclave": "2026-01-06",
    "Love Lies Bleeding": "2026-01-06",
    "Sinners": "2026-01-06",
    "Frankenstein": "2026-01-06",
    "KPop Demon Hunters": "2026-01-06",
    "Taxi Driver": "2025-05-14",
    "2001: A Space Odyssey": "2025-05-14",
    "High and Low": "2025-05-14",
    "Mad Max 2": "2025-05-14",
    "Harakiri": "2024-11-12",
    "Woman in the Dunes": "2024-11-12",
    "Kwaidan": "2024-11-12",
    "Madame Web": "2024-03-01",
    "65": "2023-05-27",
    "Blade Runner": "2019-11-08",
    "Escape from New York": "2019-11-08",
    "Captain Marvel": "2019-03-16",
    "The Great Escape": "2008-06-12",
    "North by Northwest": "2008-06-12",
    "Dawn of the Dead": "2008-06-12",
    "Cinema Paradiso": "2008-06-12",
    "Streets of Fire": "2008-06-12",
    "Deep Red": "2008-06-12",
    "Django": "2008-06-12",
}

# ---------------------------------------------------------------------------
# Curated Multi-Source Seed Dataset (Verified Hideo Kojima Movie Reviews)
# ---------------------------------------------------------------------------
SEED_RECOMMENDATIONS: List[Dict[str, Any]] = [
    {
        "title": "2001: A Space Odyssey",
        "year": "1968",
        "tmdb_id": "62",
        "director": "Stanley Kubrick",
        "runtime": "149",
        "genres": ["Science Fiction", "Mystery", "Adventure"],
        "poster": "https://image.tmdb.org/t/p/w500/ve72VxNqjGM69Uky4WTo2bK6rfq.jpg",
        "plot": "Humanity finds a mysterious object buried beneath the lunar surface and sets off to find its origins with the help of HAL 9000, the world's most advanced supercomputer.",
        "review": (
            "There's nothing left for me to say about this one. To me, it's more than a film; "
            "it's an experience."
        ),
        "sources": [
            {
                "label": "GQ All-Time Top 4",
                "type": "gq_top_4",
                "url": "https://www.slashfilm.com/1882898/hideo-kojima-favorite-movies/"
            },
            {
                "label": "Rotten Tomatoes Five Favorite Films",
                "type": "rt_top_5",
                "url": "https://editorial.rottentomatoes.com/article/death-stranding-director-hideo-kojimas-five-favorite-films/"
            },
            {
                "label": "My Favorite Films (MGS Naked Essay)",
                "type": "junkerhq_top_30",
                "url": "https://junkerhq.net/favoritefilms.html"
            }
        ]
    },
    {
        "title": "High and Low",
        "year": "1963",
        "tmdb_id": "12493",
        "director": "Akira Kurosawa",
        "runtime": "143",
        "genres": ["Crime", "Drama", "Mystery", "Thriller"],
        "poster": "https://image.tmdb.org/t/p/w500/tgNjemQPG96uIezpiUiXFcer5ga.jpg",
        "plot": "An executive of a Yokohama shoe company becomes a victim of extortion when his chauffeur's son is kidnapped by mistake and held for ransom.",
        "review": (
            "One of my favorite Kurosawa films, along with Yojimbo and Seven Samurai. "
            "Highlighted in the Criterion Closet as his #1 Akira Kurosawa masterpiece and one of his "
            "four all-time favorite films."
        ),
        "sources": [
            {
                "label": "GQ All-Time Top 4",
                "type": "gq_top_4",
                "url": "https://www.slashfilm.com/1882898/hideo-kojima-favorite-movies/"
            },
            {
                "label": "Criterion Closet Picks",
                "type": "criterion_closet",
                "url": "https://www.criterion.com/shop/collection/579-hideo-kojima-s-closet-picks"
            },
            {
                "label": "My Favorite Films (MGS Naked Essay)",
                "type": "junkerhq_top_30",
                "url": "https://junkerhq.net/favoritefilms.html"
            }
        ]
    },
    {
        "title": "Taxi Driver",
        "year": "1976",
        "tmdb_id": "103",
        "director": "Martin Scorsese",
        "runtime": "114",
        "genres": ["Crime", "Drama"],
        "poster": "https://image.tmdb.org/t/p/w500/ekstpH614fwDX8DUln1a2Opz0N8.jpg",
        "plot": "A mentally unstable Vietnam War veteran works as a night-time taxi driver in New York City where the perceived decadence and sleaze feed his urge for violent action.",
        "review": (
            "This film was an eye-opener for me. I had no idea Americans, who seemed so cheerful, "
            "could feel lonely in the middle of a city. It resonated with me, given the loneliness "
            "that troubled me in my younger days, which is why it has a very special place in my heart. "
            "After I saw it I bought a jacket and a pair of boots to copy Robert De Niro. "
            "Needless to say, I did not get a mohawk."
        ),
        "sources": [
            {
                "label": "GQ All-Time Top 4",
                "type": "gq_top_4",
                "url": "https://www.slashfilm.com/1882898/hideo-kojima-favorite-movies/"
            },
            {
                "label": "My Favorite Films (MGS Naked Essay)",
                "type": "junkerhq_top_30",
                "url": "https://junkerhq.net/favoritefilms.html"
            }
        ]
    },
    {
        "title": "Mad Max 2",
        "year": "1981",
        "tmdb_id": "8810",
        "director": "George Miller",
        "runtime": "96",
        "genres": ["Adventure", "Action", "Thriller", "Science Fiction"],
        "poster": "",
        "plot": "Max Rockatansky returns as the heroic loner who drives the dusty roads of a postapocalyptic Australian Outback in an unending search for gasoline.",
        "review": (
            "The production design, the characters, the action, the violence, the suspense, the "
            "direction—it's fantastic in every way. A classic that's in my top ten! George Miller "
            "is my first god of cinema."
        ),
        "sources": [
            {
                "label": "GQ All-Time Top 4",
                "type": "gq_top_4",
                "url": "https://www.slashfilm.com/1882898/hideo-kojima-favorite-movies/"
            },
            {
                "label": "Rotten Tomatoes Five Favorite Films",
                "type": "rt_top_5",
                "url": "https://editorial.rottentomatoes.com/article/death-stranding-director-hideo-kojimas-five-favorite-films/"
            },
            {
                "label": "My Favorite Films (MGS Naked Essay)",
                "type": "junkerhq_top_30",
                "url": "https://junkerhq.net/favoritefilms.html"
            }
        ]
    },
    {
        "title": "Blade Runner",
        "year": "1982",
        "tmdb_id": "78",
        "director": "Ridley Scott",
        "runtime": "118",
        "genres": ["Science Fiction", "Drama", "Thriller"],
        "poster": "https://image.tmdb.org/t/p/w500/63N9uy8nd9j7Eog2axPQ8lbr3Wj.jpg",
        "plot": "In the smog-choked dystopian Los Angeles of 2019, blade runner Rick Deckard is called out of retirement to terminate a quartet of replicants who have escaped to Earth seeking their creator.",
        "review": (
            "Its world and visuals are incredible. There's nothing else I need to say about this one, "
            "either. More than science fiction, it's a part of my generation's culture and shaped "
            "Snatcher and Metal Gear."
        ),
        "sources": [
            {
                "label": "My Favorite Films (MGS Naked Essay)",
                "type": "junkerhq_top_30",
                "url": "https://junkerhq.net/favoritefilms.html"
            },
            {
                "label": "Rotten Tomatoes Feature",
                "type": "rt_top_5",
                "url": "https://editorial.rottentomatoes.com/article/death-stranding-director-hideo-kojimas-five-favorite-films/"
            }
        ]
    },
    {
        "title": "Escape from New York",
        "year": "1981",
        "tmdb_id": "1103",
        "director": "John Carpenter",
        "runtime": "99",
        "genres": ["Science Fiction", "Action"],
        "poster": "https://image.tmdb.org/t/p/w500/yreqWiQ7IOkXWVB2Tz4LJIs7xqA.jpg",
        "plot": "In a world ravaged by crime, the entire island of Manhattan has been converted into a walled prison where brutal prisoners roam. When the US president crash-lands inside, war hero Snake Plissken has 24 hours to bring him out.",
        "review": (
            "There is no doubt that these films served as nutrients for the human being Hideo Kojima "
            "and became the flesh of Metal Gear Solid. Snake Plissken directly inspired Solid Snake."
        ),
        "sources": [
            {
                "label": "My Favorite Films (MGS Core Influence)",
                "type": "mgs_dna_core",
                "url": "https://junkerhq.net/favoritefilms.html"
            },
            {
                "label": "Rotten Tomatoes Feature",
                "type": "rt_top_5",
                "url": "https://editorial.rottentomatoes.com/article/death-stranding-director-hideo-kojimas-five-favorite-films/"
            }
        ]
    },
    {
        "title": "The Great Escape",
        "year": "1963",
        "tmdb_id": "5925",
        "director": "John Sturges",
        "runtime": "172",
        "genres": ["Adventure", "Drama", "History", "Thriller", "War"],
        "poster": "https://image.tmdb.org/t/p/w500/gBH4H8UMFxl139HaLz6lRuvsel8.jpg",
        "plot": "Imprisoned during World War II in a German POW camp, a group of Allied soldiers are intent on breaking out, not only to escape, but also to draw German forces away from battle to search for fugitives.",
        "review": (
            "There is no doubt that these films served as nutrients for the human being Hideo Kojima "
            "and became the flesh of Metal Gear Solid—directly shaping its stealth and POW escape mechanics."
        ),
        "sources": [
            {
                "label": "My Favorite Films (MGS Core Influence)",
                "type": "mgs_dna_core",
                "url": "https://junkerhq.net/favoritefilms.html"
            }
        ]
    },
    {
        "title": "North by Northwest",
        "year": "1959",
        "tmdb_id": "213",
        "director": "Alfred Hitchcock",
        "runtime": "136",
        "genres": ["Mystery", "Thriller", "Adventure"],
        "poster": "https://image.tmdb.org/t/p/w500/kNOFPQrel9YFCVzI0DF8FnCEpCw.jpg",
        "plot": "Advertising man Roger Thornhill is mistaken for a spy, triggering a deadly cross-country pursuit.",
        "review": (
            "There is no doubt that these films served as nutrients for the human being Hideo Kojima "
            "and became the flesh of Metal Gear Solid—shaping its espionage tension, framing, and set-piece pacing."
        ),
        "sources": [
            {
                "label": "My Favorite Films (MGS Core Influence)",
                "type": "mgs_dna_core",
                "url": "https://junkerhq.net/favoritefilms.html"
            }
        ]
    },
    {
        "title": "Dawn of the Dead",
        "year": "1978",
        "tmdb_id": "923",
        "director": "George A. Romero",
        "runtime": "127",
        "genres": ["Horror"],
        "poster": "https://image.tmdb.org/t/p/w500/h6aVbUsiJB3Le1xrhyZXsXZOI3h.jpg",
        "plot": "Following an ever-growing epidemic of zombies that have risen from the dead, two Philadelphia SWAT team members, a traffic reporter, and his television-executive girlfriend seek refuge in a secluded shopping mall.",
        "review": (
            "There is no doubt that these films served as nutrients for the human being Hideo Kojima "
            "and became the flesh of Metal Gear Solid, particularly how closed environments—elevators, "
            "shafts, ducts, and stairs—are used for tactical survival."
        ),
        "sources": [
            {
                "label": "My Favorite Films (MGS Core Influence)",
                "type": "mgs_dna_core",
                "url": "https://junkerhq.net/favoritefilms.html"
            }
        ]
    },
    {
        "title": "Cinema Paradiso",
        "year": "1988",
        "tmdb_id": "11216",
        "director": "Giuseppe Tornatore",
        "runtime": "124",
        "genres": ["Drama", "Romance"],
        "poster": "https://image.tmdb.org/t/p/w500/gCI2AeMV4IHSewhJkzsur5MEp6R.jpg",
        "plot": "A filmmaker recalls his childhood, when he fell in love with the movies at his village's theater and formed a deep friendship with the theater's projectionist.",
        "review": (
            "This film also makes me cry—particularly the final scene, but in a way I never expected. "
            "A must-see for movie lovers. The child is good, too."
        ),
        "sources": [
            {
                "label": "My Favorite Films (MGS Naked Essay)",
                "type": "junkerhq_top_30",
                "url": "https://junkerhq.net/favoritefilms.html"
            }
        ]
    },
    {
        "title": "Streets of Fire",
        "year": "1984",
        "tmdb_id": "14746",
        "director": "Walter Hill",
        "runtime": "93",
        "genres": ["Action", "Crime", "Music", "Romance", "Thriller"],
        "poster": "",
        "plot": "A mercenary is hired to rescue his ex-girlfriend, a singer who has been kidnapped by a motorcycle gang.",
        "review": (
            "A coming-of-age rock 'n' roll film with action, violence and romance. It's new wave "
            "music meets MTV. It instantly made me a fan of Diane Lane. There was a time when I "
            "would watch its ending every morning."
        ),
        "sources": [
            {
                "label": "My Favorite Films (MGS Naked Essay)",
                "type": "junkerhq_top_30",
                "url": "https://junkerhq.net/favoritefilms.html"
            }
        ]
    },
    {
        "title": "Deep Red",
        "year": "1975",
        "tmdb_id": "20126",
        "director": "Dario Argento",
        "runtime": "127",
        "genres": ["Horror", "Mystery", "Thriller"],
        "poster": "",
        "plot": "A jazz pianist and a wisecracking journalist are pulled into a complex web of mystery after the former witnesses the brutal murder of a psychic.",
        "review": (
            "This is the greatest of all Argento's films, and it's my number-one horror film. "
            "Goblin's music is cool. The revelation at the end is scary."
        ),
        "sources": [
            {
                "label": "My Favorite Films (MGS Naked Essay)",
                "type": "junkerhq_top_30",
                "url": "https://junkerhq.net/favoritefilms.html"
            }
        ]
    },
    {
        "title": "Django",
        "year": "1966",
        "tmdb_id": "10772",
        "director": "Sergio Corbucci",
        "runtime": "91",
        "genres": ["Action", "Western"],
        "poster": "",
        "plot": "A coffin-dragging gunslinger enters a town caught between two ruthless factions: a band of racist Confederates and a gang of Mexican revolutionaries.",
        "review": (
            "I also love Spaghetti Westerns. This is what led to Ocelot, among other things, and "
            "this film is my favorite out of all of them. Django's songs are good. He was the "
            "inspiration for Boktai's main character."
        ),
        "sources": [
            {
                "label": "My Favorite Films (MGS Naked Essay)",
                "type": "junkerhq_top_30",
                "url": "https://junkerhq.net/favoritefilms.html"
            }
        ]
    },
    {
        "title": "Harakiri",
        "year": "1962",
        "tmdb_id": "14537",
        "director": "Masaki Kobayashi",
        "runtime": "135",
        "genres": ["Action", "Drama", "History"],
        "poster": "https://image.tmdb.org/t/p/w500/5konZnIbcAxZjP616Cz5o9bKEfW.jpg",
        "plot": "Down-on-his-luck veteran Tsugumo Hanshiro enters the courtyard of the prosperous House of Iyi. Unemployed, and with no family, he hopes to find a place to commit seppuku.",
        "review": (
            "Hand-picked by Hideo Kojima in the Criterion Closet as a towering masterpiece of 1960s "
            "Japanese cinema by Masaki Kobayashi—blending stark critique of samurai honor, theatrical "
            "framing, and existential suspense."
        ),
        "sources": [
            {
                "label": "Criterion Closet Picks",
                "type": "criterion_closet",
                "url": "https://www.criterion.com/shop/collection/579-hideo-kojima-s-closet-picks"
            }
        ]
    },
    {
        "title": "Woman in the Dunes",
        "year": "1964",
        "tmdb_id": "16672",
        "director": "Hiroshi Teshigahara",
        "runtime": "147",
        "genres": ["Drama", "Thriller"],
        "poster": "",
        "plot": "An entomologist suffers extreme physical and psychological trauma after being taken captive by the residents of a poor seaside village and made to live with a woman whose life task is shoveling sand.",
        "review": (
            "Selected in the Criterion Closet and analyzed in Kojima's book 'The Creative Gene': while "
            "most stories feature a protagonist who falls into a hole and either crawls out or dies, "
            "Hiroshi Teshigahara and Kobo Abe's existential masterpiece offers a brilliant third "
            "possibility—finding a life and purpose inside the hole."
        ),
        "sources": [
            {
                "label": "Criterion Closet Picks",
                "type": "criterion_closet",
                "url": "https://www.criterion.com/shop/collection/579-hideo-kojima-s-closet-picks"
            }
        ]
    },
    {
        "title": "Kwaidan",
        "year": "1964",
        "tmdb_id": "30959",
        "director": "Masaki Kobayashi",
        "runtime": "183",
        "genres": ["Fantasy", "Horror", "Drama"],
        "poster": "",
        "plot": "Taking its title from an archaic Japanese word meaning 'ghost story,' this anthology adapts four folk tales of the supernatural.",
        "review": (
            "Hand-picked in Hideo Kojima's Criterion Closet Picks as an atmospheric, surreal anthology "
            "masterpiece of Japanese ghost stories by Masaki Kobayashi that shaped post-war Japanese visual storytelling."
        ),
        "sources": [
            {
                "label": "Criterion Closet Picks",
                "type": "criterion_closet",
                "url": "https://www.criterion.com/shop/collection/579-hideo-kojima-s-closet-picks"
            }
        ]
    },
    {
        "title": "Avatar: Fire and Ash",
        "year": "2025",
        "tmdb_id": "83533",
        "director": "James Cameron",
        "runtime": "192",
        "genres": ["Science Fiction", "Adventure", "Action"],
        "poster": "",
        "plot": "Jake Sully and Neytiri encounter a new aggressive Na'vi tribe known as the Ash People on Pandora.",
        "review": (
            "This film shows absolutely no compromise, right down to the finest details, in the "
            "construction of the Avatar 'universe.' It was magnificent."
        ),
        "sources": [
            {
                "label": "2025/2026 Review & Favorites Roundup",
                "type": "x_review",
                "url": "https://geektyrant.com/news/hideo-kojima-lists-his-favorite-movies-and-shows-of-2025-and-they-include-kpop-demon-hunters-and-pluribus"
            }
        ]
    },
    {
        "title": "Conclave",
        "year": "2024",
        "tmdb_id": "974576",
        "director": "Edward Berger",
        "runtime": "120",
        "genres": ["Drama", "Thriller", "Mystery"],
        "poster": "",
        "plot": "After the unexpected death of the Pope, Cardinal Lawrence is tasked with managing the covert and ancient ritual of electing a new one.",
        "review": (
            "\"These are the two best films I have seen in January and February. Both of them have an "
            "incredible sense of filmmaking.\" Named to Hideo Kojima's Top Foreign Theatrical Releases of the Year list."
        ),
        "sources": [
            {
                "label": "Top Films of 2025 (Instagram / X)",
                "type": "annual_top_list",
                "url": "https://www.instagram.com/p/DTKMr5rkvfH/?hl=en"
            },
            {
                "label": "GeekTyrant 2025 Favorites Report",
                "type": "annual_top_list",
                "url": "https://geektyrant.com/news/hideo-kojima-lists-his-favorite-movies-and-shows-of-2025-and-they-include-kpop-demon-hunters-and-pluribus"
            }
        ]
    },
    {
        "title": "Love Lies Bleeding",
        "year": "2024",
        "tmdb_id": "948549",
        "director": "Rose Glass",
        "runtime": "104",
        "genres": ["Crime", "Romance", "Thriller"],
        "poster": "",
        "plot": "Reclusive gym manager Lou falls hard for Jackie, an ambitious bodybuilder headed through town to Las Vegas in pursuit of her dream.",
        "review": (
            "\"Love Lies Bleeding, the latest film from super promising director Rose Glass (Saint Maud) "
            "and produced by A24, is too good to be true! Kristen Stewart is awesome, but our very own "
            "Ed Harris is a heartthrob! Too cool! Already looks like there could be a spin-off!\""
        ),
        "sources": [
            {
                "label": "Top Films of 2025 (Instagram / X)",
                "type": "annual_top_list",
                "url": "https://www.instagram.com/p/DTKMr5rkvfH/?hl=en"
            },
            {
                "label": "GeekTyrant 2025 Favorites Report",
                "type": "annual_top_list",
                "url": "https://geektyrant.com/news/hideo-kojima-lists-his-favorite-movies-and-shows-of-2025-and-they-include-kpop-demon-hunters-and-pluribus"
            }
        ]
    },
    {
        "title": "Sinners",
        "year": "2025",
        "tmdb_id": "1233413",
        "director": "Ryan Coogler",
        "runtime": "137",
        "genres": ["Horror", "Drama", "Thriller"],
        "poster": "",
        "plot": "Trying to leave their troubled lives behind, twin brothers return to their hometown to start again, only to discover that an even greater evil is waiting to welcome them back.",
        "review": (
            "\"One of my top two favorite movies I have seen this year.\" Highlighted in Hideo Kojima's "
            "Mid-Year Top 4 and Year-End Top Theatrical Releases of the Year lists."
        ),
        "sources": [
            {
                "label": "Top Films of 2025 (Instagram / X)",
                "type": "annual_top_list",
                "url": "https://www.instagram.com/p/DTKMr5rkvfH/?hl=en"
            },
            {
                "label": "GeekTyrant 2025 Favorites Report",
                "type": "annual_top_list",
                "url": "https://geektyrant.com/news/hideo-kojima-lists-his-favorite-movies-and-shows-of-2025-and-they-include-kpop-demon-hunters-and-pluribus"
            }
        ]
    },
    {
        "title": "Frankenstein",
        "year": "2025",
        "tmdb_id": "1062722",
        "director": "Guillermo del Toro",
        "runtime": "150",
        "genres": ["Drama", "Horror", "Science Fiction"],
        "poster": "",
        "plot": "Dr. Pretorius tracks down Frankenstein's monster, who is believed to have died in a fire forty years before, in order to continue the experiments of Dr. Victor Frankenstein.",
        "review": (
            "Singled out by Hideo Kojima as one of his favorite streaming films of the year and celebrated "
            "on stage with longtime creative collaborator Guillermo del Toro at the Tokyo premiere, praising "
            "its visually rich gothic horror vision."
        ),
        "sources": [
            {
                "label": "Top Films of 2025 (Instagram / X)",
                "type": "annual_top_list",
                "url": "https://www.instagram.com/p/DTKMr5rkvfH/?hl=en"
            },
            {
                "label": "GeekTyrant 2025 Favorites Report",
                "type": "annual_top_list",
                "url": "https://geektyrant.com/news/hideo-kojima-lists-his-favorite-movies-and-shows-of-2025-and-they-include-kpop-demon-hunters-and-pluribus"
            }
        ]
    },
    {
        "title": "KPop Demon Hunters",
        "year": "2025",
        "tmdb_id": "803796",
        "director": "Maggie Kang",
        "runtime": "96",
        "genres": ["Animation", "Action", "Fantasy", "Music"],
        "poster": "",
        "plot": "A world-renowned K-Pop girl group balances their lives in the spotlight with their secret identities as badass demon hunters.",
        "review": (
            "\"I casually started watching 'KPop Demon Hunters,' got completely hooked, and ended up "
            "crying my eyes out by the end. It was so, so good.\""
        ),
        "sources": [
            {
                "label": "Top Films of 2025 (Instagram / X)",
                "type": "annual_top_list",
                "url": "https://www.instagram.com/p/DTKMr5rkvfH/?hl=en"
            },
            {
                "label": "GeekTyrant 2025 Favorites Report",
                "type": "annual_top_list",
                "url": "https://geektyrant.com/news/hideo-kojima-lists-his-favorite-movies-and-shows-of-2025-and-they-include-kpop-demon-hunters-and-pluribus"
            }
        ]
    },
    {
        "title": "Madame Web",
        "year": "2024",
        "tmdb_id": "634492",
        "director": "S.J. Clarkson",
        "runtime": "116",
        "genres": ["Action", "Fantasy"],
        "poster": "",
        "plot": "Forced to confront revelations about her past, paramedic Cassandra Webb forges a relationship with three young women destined for powerful futures.",
        "review": "Saw Madame Web at the theater.",
        "sources": [
            {
                "label": "GameSpot One-Sentence Review Archive",
                "type": "x_review",
                "url": "https://www.gamespot.com/gallery/every-hideo-kojima-one-sentence-movie-review/2900-5174/"
            }
        ]
    },
    {
        "title": "Captain Marvel",
        "year": "2019",
        "tmdb_id": "299537",
        "director": "Anna Boden",
        "runtime": "124",
        "genres": ["Action", "Adventure", "Science Fiction"],
        "poster": "",
        "plot": "The story follows Carol Danvers as she becomes one of the universe's most powerful heroes when Earth is caught in the middle of a galactic war between two alien races.",
        "review": "Saw Captain Marvel.",
        "sources": [
            {
                "label": "GameSpot One-Sentence Review Archive",
                "type": "x_review",
                "url": "https://www.gamespot.com/gallery/every-hideo-kojima-one-sentence-movie-review/2900-5174/"
            }
        ]
    },
    {
        "title": "65",
        "year": "2023",
        "tmdb_id": "700391",
        "director": "Scott Beck",
        "runtime": "93",
        "genres": ["Science Fiction", "Adventure", "Thriller"],
        "poster": "",
        "plot": "65 million years ago, the only 2 survivors of a spaceship from Somaris that crash-landed on Earth must fend off dinosaurs and reach the escape vessel in time before an imminent asteroid strike.",
        "review": "Watched '65' since Adam Driver and the dinosaur are starring.",
        "sources": [
            {
                "label": "GameSpot One-Sentence Review Archive",
                "type": "x_review",
                "url": "https://www.gamespot.com/gallery/every-hideo-kojima-one-sentence-movie-review/2900-5174/"
            }
        ]
    }
]

# ---------------------------------------------------------------------------
# Evidence-Aware Multi-Factor Enthusiasm Scoring Engine
# ---------------------------------------------------------------------------
ONE_SENTENCE_DISMISSAL_RE = re.compile(
    r"^(?:i\s+)?(?:went\s+to\s+see|saw|watched|finally\s+got\s+to\s+see)\b[^.!?]*[.!?]?$",
    re.IGNORECASE,
)

EDITORIAL_LIST_BLURB_RE = re.compile(
    r"^(?:named\s+(?:to|by)|selected\s+(?:as|in|by)|highlighted\s+(?:on|in)|picked\s+by|singled\s+out\s+by|featured\s+in|one\s+of\s+the\s+seven)\b",
    re.IGNORECASE,
)

FIRST_PERSON_VOICE_RE = re.compile(
    r"""(?:\b(?:i|my|me|mine)\b|["“”])""",
    re.IGNORECASE,
)

HIGH_PRAISE_TERMS = [
    "masterpiece", "magnificent", "incredible", "amazing", "fantastic", "greatest",
    "favorite", "must-see", "must-watch", "cry", "crying", "tearjerker", "speechless",
    "eye-opener", "more than a film", "experience", "no compromise", "burned into my brain",
    "god of cinema", "first god", "too good to be true", "so, so good", "completely hooked",
    "hooked", "best films", "best movie", "top two", "top four", "top ten", "number-one",
    "number one", "awesome", "too cool", "towering", "brilliant",
]

CRAFT_AND_AUTEUR_TERMS = [
    "sense of filmmaking", "production design", "direction", "visuals", "visually rich",
    "finest details", "world", "universe", "suspense", "cinematography", "framing", "music",
    "ending", "final scene", "spin-off", "promising director", "del toro", "kurosawa",
    "kubrick", "scorsese", "miller", "carpenter", "argento", "kobayashi", "teshigahara",
    "kobo abe", "cameron", "coogler", "rose glass", "berger", "storytelling",
]

PERSONAL_AND_DNA_TERMS = [
    "my heart", "when i was a kid", "my generation", "my father", "my mother",
    "loneliness", "every morning", "mentor", "bought a jacket", "by myself",
    "mgs", "metal gear", "snatcher", "death stranding", "boktai", "ocelot",
    "solid snake", "stealth", "shaped", "influence", "influenced", "inspired",
    "nutrients", "flesh of", "creative gene", "70%",
]

CURATED_CANON_TYPES = {
    "gq_top_4", "rt_top_5", "mgs_dna_core", "junkerhq_top_30", "criterion_closet"
}

ALL_CURATED_TYPES = CURATED_CANON_TYPES | {"annual_top_list", "junkerhq_runner_up"}


@dataclass
class ScoreBreakdown:
    total_score: int
    tier: str
    word_count_pts: float
    sentiment_pts: float
    prestige_pts: float
    recurrence_pts: float
    is_dismissal: bool
    sentiment_summary: str


def is_one_sentence_dismissal(review_text: str, source_types: List[str]) -> bool:
    """
    Detects Kojima's signature 'I watched [Movie] at the theater.' negative non-review.
    If the movie appears on any curated top/favorite list, it is never a dismissal.
    """
    if any(t in ALL_CURATED_TYPES for t in source_types):
        return False

    words = review_text.strip().split()
    if len(words) <= 15 and ONE_SENTENCE_DISMISSAL_RE.match(review_text.strip()):
        return True
    return False


def is_editorial_list_blurb(review_text: str, source_types: List[str]) -> bool:
    """
    Detects when the stored review string is a third-person editorial list attribution
    (e.g., 'Named to Hideo Kojima's Top Foreign Theatrical Releases of the Year list.')
    rather than a verbatim first-person Kojima quote.
    """
    if not any(t in ALL_CURATED_TYPES for t in source_types):
        return False
    stripped = review_text.strip()
    if not stripped:
        return True
    if EDITORIAL_LIST_BLURB_RE.match(stripped) and not FIRST_PERSON_VOICE_RE.search(stripped):
        return True
    return False


def analyze_sentiment_with_llm(
    title: str,
    review_text: str,
    sources: Optional[List[Dict[str, str]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Calls OpenAI, Gemini, or local Ollama to score sentiment & depth (0-35 pts).
    Returns None if LLM_PROVIDER is 'heuristic' or if the network call fails.
    """
    if LLM_PROVIDER == "heuristic":
        return None

    source_labels = ", ".join(s.get("label", s.get("type", "")) for s in (sources or []))
    prompt = (
        f"Analyze Hideo Kojima's enthusiasm for the movie '{title}'.\n"
        f"Curated Sources: {source_labels or 'Standalone X Review'}\n"
        f"Review / List Commentary:\n\"\"\"{review_text}\"\"\"\n\n"
        "Note: Kojima watches 250+ films a year; inclusion on his Year-End Top List, Criterion Closet, "
        "or All-Time Favorite lists is itself a major signal of high enthusiasm even if the text is a "
        "third-person list summary.\n"
        "Score on three dimensions and return strict JSON:\n"
        "- praise_intensity (0 to 15): Emotional resonance, superlative praise, or list selectivity.\n"
        "- craft_and_personal (0 to 10): Filmmaking craft admiration or personal reflection.\n"
        "- creative_resonance (0 to 10): Connection to his creative DNA, favorite auteurs, or canon.\n"
        "- summary (string): 1-sentence summary of his enthusiasm signals."
    )

    try:
        if LLM_PROVIDER == "openai" and OPENAI_API_KEY:
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=json.dumps({
                    "model": OPENAI_MODEL,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                }).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return json.loads(body["choices"][0]["message"]["content"])

        if LLM_PROVIDER == "gemini" and GEMINI_API_KEY:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
            )
            req = urllib.request.Request(
                url,
                data=json.dumps({
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json"},
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                text = body["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)

        if LLM_PROVIDER == "ollama":
            req = urllib.request.Request(
                OLLAMA_URL,
                data=json.dumps({
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "format": "json",
                    "stream": False,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return json.loads(body["response"])
    except Exception as exc:
        print(f"[warn] LLM sentiment analysis failed for {title}, using heuristic fallback: {exc}")
    return None


def _curated_sentiment_baseline(source_types: List[str], editorial_blurb: bool) -> float:
    """
    Returns the baseline sentiment score (out of 35) implied by the highest-selectivity
    curated list the movie belongs to. When the stored text is only a third-person
    editorial list attribution (e.g., 'Named to Hideo Kojima's Top Foreign Theatrical Releases...'),
    a slightly higher floor is applied so missing quote text does not penalize list picks.
    """
    if any(t in {"gq_top_4", "rt_top_5"} for t in source_types):
        return 30.0 if editorial_blurb else 27.5
    if "mgs_dna_core" in source_types:
        return 28.5 if editorial_blurb else 25.5
    if any(t in {"junkerhq_top_30", "criterion_closet"} for t in source_types):
        return 27.5 if editorial_blurb else 23.5
    if "annual_top_list" in source_types:
        return 27.5 if editorial_blurb else 23.0
    if "junkerhq_runner_up" in source_types:
        return 22.0 if editorial_blurb else 19.0
    return 16.0


def compute_enthusiasm_score(
    title: str,
    review_text: str,
    sources: List[Dict[str, str]],
) -> ScoreBreakdown:
    """
    Computes the 0-100 Evidence-Aware Multi-Factor Kojima Enthusiasm Score:
      1. Elaboration & List Selectivity Curve (W: 0 - 30 pts)
         — Penalizes 1-sentence 'I watched X' dismissals (0 pts).
         — Rewards long-form commentary while applying selectivity floors for curated canon/annual lists.
      2. Multi-Signal Sentiment, Craft & Creative Resonance (S: 0 - 35 pts)
         — Evaluates Superlative Praise, Filmmaking Craft/Auteur admiration, and Personal/Creative DNA,
           combined with the implicit enthusiasm baseline of any curated list selection.
      3. Curated Prestige & Canon Bonus (P: 0 - 25 pts)
      4. Cross-Source & Canon Recurrence Bonus (R: 0 - 10 pts)
    """
    source_types = [s.get("type", "x_review") for s in sources] if sources else ["x_review"]
    dismissal = is_one_sentence_dismissal(review_text, source_types)
    editorial_blurb = is_editorial_list_blurb(review_text, source_types)
    is_all_time_canon = any(t in CURATED_CANON_TYPES for t in source_types)
    is_top_five = any(t in {"gq_top_4", "rt_top_5"} for t in source_types)

    words = review_text.strip().split()
    word_count = len(words)

    lower_text = review_text.lower()
    praise_hits = sum(1 for term in HIGH_PRAISE_TERMS if term in lower_text)
    craft_hits = sum(1 for term in CRAFT_AND_AUTEUR_TERMS if term in lower_text)
    dna_hits = sum(1 for term in PERSONAL_AND_DNA_TERMS if term in lower_text)
    exclamations = review_text.count("!")

    # 1. Word-Count / Elaboration & Selectivity Curve (0 - 30 pts)
    if dismissal:
        word_pts = 0.0
    else:
        base_word_pts = min(30.0, round(7.2 * math.log(word_count + 1), 1))
        if is_top_five:
            word_pts = max(26.5, base_word_pts)
        elif "mgs_dna_core" in source_types:
            word_pts = max(25.0 if editorial_blurb else 24.5, base_word_pts)
        elif is_all_time_canon or "annual_top_list" in source_types:
            word_pts = max(24.5 if editorial_blurb else 23.5, base_word_pts)
        else:
            word_pts = base_word_pts

    # 2. Sentiment, Craft & Creative Resonance (0 - 35 pts)
    baseline_sentiment = _curated_sentiment_baseline(source_types, editorial_blurb)
    llm_result = None if dismissal else analyze_sentiment_with_llm(title, review_text, sources)
    if llm_result:
        praise = min(15.0, float(llm_result.get("praise_intensity", 0)))
        craft_personal = min(
            10.0,
            float(llm_result.get("craft_and_personal", llm_result.get("personal_connection", 0))),
        )
        influence = min(
            10.0,
            float(llm_result.get("creative_resonance", llm_result.get("creative_influence", 0))),
        )
        sentiment_pts = min(35.0, max(baseline_sentiment, round(praise + craft_personal + influence, 1)))
        sentiment_summary = str(llm_result.get("summary", "AI-scored review sentiment."))
    elif dismissal:
        sentiment_pts = 2.0
        sentiment_summary = "Infamous 1-sentence 'I watched it' non-review (no praise commentary)."
    else:
        is_standalone_x = not any(t in ALL_CURATED_TYPES for t in source_types)
        praise_weight = 4.0 if is_standalone_x else 3.0
        craft_weight = 2.5 if is_standalone_x else 2.0
        signal_boost = (
            praise_hits * praise_weight
            + craft_hits * craft_weight
            + dna_hits * 3.0
            + min(4.5, exclamations * 1.5)
        )
        sentiment_pts = min(35.0, round(baseline_sentiment + signal_boost, 1))

        if is_top_five:
            list_context = "All-Time Top 4/5 canon selection"
        elif "mgs_dna_core" in source_types:
            list_context = "Core Metal Gear Solid DNA influence"
        elif "criterion_closet" in source_types:
            list_context = "Criterion Closet hand-picked classic"
        elif "junkerhq_top_30" in source_types:
            list_context = "All-Time Top 30 personal favorite"
        elif "annual_top_list" in source_types:
            list_context = "Year-End Top Movies of the Year selection"
        else:
            list_context = "Standalone review"

        if editorial_blurb and (praise_hits + craft_hits + dna_hits) == 0:
            sentiment_summary = (
                f"{list_context} (editorial list attribution; scored via curated list selectivity)."
            )
        else:
            sentiment_summary = (
                f"{list_context} — {praise_hits} superlative praise, "
                f"{craft_hits} craft/auteur, and {dna_hits} personal/DNA markers."
            )

    # 3. Curated Prestige & Canon Bonus (0 - 25 pts)
    if dismissal:
        prestige_pts = 5.0
    else:
        base_prestige = max((SOURCE_PRESTIGE_WEIGHTS.get(t, 10) for t in source_types), default=10)
        # Standalone X raves with strong praise/craft commentary get a prestige boost (10 -> 14)
        if base_prestige == 10 and (praise_hits >= 2 or (praise_hits >= 1 and craft_hits >= 1)):
            prestige_pts = 14.0
        else:
            prestige_pts = float(base_prestige)

    # 4. Cross-Source & Canon Recurrence Bonus (0 - 10 pts)
    if dismissal:
        recurrence_pts = 0.0
    else:
        unique_urls = {s.get("url", "") for s in sources if s.get("url")}
        url_bonus = float(min(10, max(0, (len(unique_urls) - 1) * 5)))
        # Lifelong canon entries (MGS DNA, Junker HQ Top 30, Criterion Closet) represent
        # recurring multi-year citations even when indexed under a single archive URL.
        if url_bonus == 0.0 and is_all_time_canon:
            recurrence_pts = 4.0
        elif url_bonus == 0.0 and "annual_top_list" in source_types:
            recurrence_pts = 3.0
        else:
            recurrence_pts = url_bonus

    raw_total = round(word_pts + sentiment_pts + prestige_pts + recurrence_pts)
    total = min(20, raw_total) if dismissal else min(100, raw_total)

    if total >= 90:
        tier = "S-Tier: Essential Kojima DNA"
    elif total >= 75:
        tier = "A-Tier: All-Time Favorite / Rave"
    elif total >= 55:
        tier = "B-Tier: Strong Recommendation"
    elif total >= 25:
        tier = "C-Tier: Notable Mention"
    else:
        tier = "D-Tier: One-Sentence Dismissal"

    return ScoreBreakdown(
        total_score=total,
        tier=tier,
        word_count_pts=word_pts,
        sentiment_pts=sentiment_pts,
        prestige_pts=prestige_pts,
        recurrence_pts=recurrence_pts,
        is_dismissal=dismissal,
        sentiment_summary=sentiment_summary,
    )


# ---------------------------------------------------------------------------
# TMDB ID & Metadata Resolver (for newly scraped X posts or missing posters)
# ---------------------------------------------------------------------------
def resolve_tmdb_id(title: str, year: Optional[str] = None) -> Optional[str]:
    if not TMDB_API_KEY:
        return None
    query = urllib.parse.quote(title)
    url = f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&query={query}"
    if year and year != "0000":
        url += f"&year={urllib.parse.quote(year)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results") or []
            if results:
                return str(results[0]["id"])
    except Exception as exc:
        print(f"[warn] TMDB lookup failed for {title}: {exc}")
    return None


def fetch_tmdb_metadata(tmdb_id: str) -> Dict[str, Any]:
    """
    When TMDB_API_KEY is set, fetches movie details + credits from TMDB v3 API
    to populate poster, director, runtime, genres, and plot.
    """
    if not TMDB_API_KEY or not tmdb_id:
        return {}
    url = (
        f"https://api.themoviedb.org/3/movie/{urllib.parse.quote(str(tmdb_id))}"
        f"?api_key={TMDB_API_KEY}&append_to_response=credits"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        poster_path = data.get("poster_path") or ""
        poster = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
        crew = (data.get("credits") or {}).get("crew") or []
        directors = [c.get("name", "") for c in crew if c.get("job") == "Director" and c.get("name")]
        genres = [g.get("name", "") for g in (data.get("genres") or []) if g.get("name")]
        runtime = str(data.get("runtime") or "")
        plot = str(data.get("overview") or "")
        release_date = str(data.get("release_date") or "")
        year = release_date[:4] if len(release_date) >= 4 else ""
        return {
            "poster": poster,
            "director": ", ".join(directors),
            "genres": genres,
            "runtime": runtime,
            "plot": plot,
            "year": year,
        }
    except Exception as exc:
        print(f"[warn] TMDB metadata fetch failed for {tmdb_id}: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Post / RSS Movie Title & Review Extractor
# ---------------------------------------------------------------------------
POST_PATTERNS = [
    # e.g. "Watched 'Furiosa: A Mad Max Saga' (2024) at the theater."
    re.compile(
        r"""(?:i\s+)?(?:went\s+to\s+(?:see|the\s+[\w\s\-]+premiere\s+of)|saw|watched|rewatch(?:ed|ing)?|finally\s+got\s+to\s+(?:see|watch)|screened)\s+['"“”『「](?P<title>[^'"“”』」]{2,80})['"“”』」](?:\s*\((?P<year>\d{4})\))?""",
        re.IGNORECASE,
    ),
    # e.g. "I went to see Darren Aronofsky’s “Caught Stealing.”" or "I watched Christopher Nolan’s “The Odyssey”"
    # or "Director Iñárritu’s “DIGGER” is a film..." or "curious about Patrick Hughes’ “War Machine.”"
    re.compile(
        r"""(?:went\s+to\s+(?:see|the\s+[\w\s\-]+premiere\s+of)|saw|watched|rewatch(?:ed|ing)?|finally\s+got\s+to\s+(?:see|watch)|screened|curious\s+about|director\s+[A-Z][\w\s.\-’'ñáéíóú]{1,35}[’']s)\b[^.!?'"“”『「\n]{0,65}?['"“”『「](?P<title>[^'"“”』」]{2,80})['"“”』」](?:\s*\((?P<year>\d{4})\))?""",
        re.IGNORECASE,
    ),
    # e.g. "Saw Madame Web at the theater." or "Saw Captain Marvel."
    re.compile(
        r"""^(?:i\s+)?(?:went\s+to\s+see|saw|watched|finally\s+got\s+to\s+see)\s+(?P<title>[A-Z0-9][\w\s:,'\-&!]{1,60}?)(?:\s+(?:at\s+the\s+theater|in\s+imax|on\s+plane|since\b|with\b|today\b)|[.!?]$)""",
        re.IGNORECASE,
    ),
    # Letterboxd style: "Conclave, 2024 - ★★★★"
    re.compile(
        r"""^(?P<title>[^,\n]{2,70}),\s*(?P<year>(?:19|20)\d{2})\b""",
    ),
]


def _clean_extracted_title(raw_title: str) -> str:
    title = raw_title.strip(" .,'\"“”!?")
    # Strip inline format notes like "Odyssey (entirely shot on IMAX® film cameras)"
    title = re.sub(r"\s*\((?:entirely\s+shot|shot\s+entirely|4k|imax|3d)[^)]*\)$", "", title, flags=re.IGNORECASE).strip(" .,'\"“”!?")
    return title


def extract_movie_from_post(raw_text: str) -> Optional[Tuple[str, str, str]]:
    """
    Attempts to extract (title, year, cleaned_review) from an X/Nitter or Letterboxd post.
    Returns None if no movie mention is detected.
    """
    cleaned = html.unescape(re.sub(r"<[^>]+>", " ", raw_text)).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return None

    for pattern in POST_PATTERNS:
        m = pattern.search(cleaned)
        if m:
            title = _clean_extracted_title(m.group("title"))
            year = (m.groupdict().get("year") or "0000").strip()
            if title and len(title) >= 2:
                return (title, year, cleaned)

    return None


# ---------------------------------------------------------------------------
# SQLite Persistence Layer (`/data/kojima_reviews.db`)
# ---------------------------------------------------------------------------
_DB_LOCK = threading.Lock()
_LAST_SCRAPE_AT: Optional[str] = None


def make_slug(title: str, year: str = "0000") -> str:
    norm_title = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    norm_year = (year or "0000").strip()
    return f"{norm_title}::{norm_year}"


def get_db_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    target_path = db_path or KOJIMA_DB_PATH
    parent = os.path.dirname(os.path.abspath(target_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(target_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    with _DB_LOCK:
        conn = get_db_connection(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS recommendations (
                    slug TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    year TEXT NOT NULL,
                    tmdb_id TEXT NOT NULL,
                    director TEXT DEFAULT '',
                    runtime TEXT DEFAULT '',
                    genres_json TEXT DEFAULT '[]',
                    poster TEXT DEFAULT '',
                    plot TEXT DEFAULT '',
                    review TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    kojima_score INTEGER NOT NULL,
                    kojima_tier TEXT NOT NULL,
                    word_count_pts REAL NOT NULL,
                    sentiment_pts REAL NOT NULL,
                    prestige_pts REAL NOT NULL,
                    recurrence_pts REAL NOT NULL,
                    is_dismissal TEXT NOT NULL,
                    sentiment_summary TEXT NOT NULL,
                    score_breakdown TEXT NOT NULL,
                    primary_url TEXT NOT NULL,
                    primary_source TEXT NOT NULL,
                    recommended_at TEXT DEFAULT '',
                    llm_provider TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scrape_log (
                    guid TEXT PRIMARY KEY,
                    feed_url TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    extracted_title TEXT,
                    tmdb_id TEXT,
                    scraped_at TEXT NOT NULL
                );
                """
            )
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(recommendations)").fetchall()
            }
            if "recommended_at" not in cols:
                conn.execute(
                    "ALTER TABLE recommendations ADD COLUMN recommended_at TEXT DEFAULT ''"
                )
            conn.commit()
        finally:
            conn.close()


def _infer_recommended_at(title: str, item: Dict[str, Any], sources: List[Dict[str, str]]) -> str:
    explicit = str(item.get("recommended_at") or item.get("kojima_recommended_at") or "").strip()
    if explicit:
        return explicit[:10]
    if title in MOVIE_RECOMMENDED_DATES:
        return MOVIE_RECOMMENDED_DATES[title]
    for src in sources:
        src_date = str(src.get("date") or "").strip()
        if src_date:
            return src_date[:10]
        src_type = str(src.get("type") or "").strip()
        if src_type in SOURCE_DEFAULT_DATES:
            return SOURCE_DEFAULT_DATES[src_type]
    return time.strftime("%Y-%m-%d", time.gmtime())


SCORING_ENGINE_VERSION = "v2.1"


def upsert_recommendation(
    item: Dict[str, Any],
    force_rescore: bool = False,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Computes (or reuses cached) enthusiasm score for a movie item and persists to SQLite.
    """
    init_db(db_path)
    title = str(item["title"]).strip()
    year = str(item.get("year") or "0000").strip()
    slug = make_slug(title, year)
    review_text = str(item.get("review") or "").strip()
    sources: List[Dict[str, str]] = item.get("sources") or []
    recommended_at = _infer_recommended_at(title, item, sources)
    sources_json = json.dumps(sources, sort_keys=True)
    content_hash = hashlib.sha256(
        f"{SCORING_ENGINE_VERSION}|{title}|{year}|{review_text}|{sources_json}|{recommended_at}|{LLM_PROVIDER}".encode("utf-8")
    ).hexdigest()

    with _DB_LOCK:
        conn = get_db_connection(db_path)
        try:
            existing = conn.execute(
                "SELECT * FROM recommendations WHERE slug = ?", (slug,)
            ).fetchone()

            if existing and not force_rescore and existing["content_hash"] == content_hash:
                # If tmdb_id was missing previously and we can resolve it now, update it
                cached_tmdb_id = existing["tmdb_id"] or item.get("tmdb_id") or resolve_tmdb_id(title, year) or ""
                if cached_tmdb_id != existing["tmdb_id"]:
                    conn.execute(
                        "UPDATE recommendations SET tmdb_id = ? WHERE slug = ?",
                        (str(cached_tmdb_id), slug),
                    )
                    conn.commit()
                # Backfill poster/plot discovered after the original scoring run
                if not existing["poster"] and item.get("poster"):
                    conn.execute(
                        "UPDATE recommendations SET poster = ?, plot = CASE WHEN plot = '' THEN ? ELSE plot END WHERE slug = ?",
                        (str(item["poster"]), str(item.get("plot") or ""), slug),
                    )
                    conn.commit()
                row = conn.execute("SELECT * FROM recommendations WHERE slug = ?", (slug,)).fetchone()
                return _row_to_feed_dict(row)

            # Merge sources if movie already exists from another source
            if existing:
                try:
                    old_sources = json.loads(existing["sources_json"])
                except Exception:
                    old_sources = []
                seen_urls = {s.get("url", "") for s in sources}
                for old_src in old_sources:
                    if old_src.get("url", "") not in seen_urls:
                        sources.append(old_src)
                        seen_urls.add(old_src.get("url", ""))
                sources_json = json.dumps(sources)

            tmdb_id = str(item.get("tmdb_id") or (existing["tmdb_id"] if existing else "") or resolve_tmdb_id(title, year) or "")
            director = str(item.get("director") or (existing["director"] if existing else "") or "")
            runtime = str(item.get("runtime") or (existing["runtime"] if existing else "") or "")
            genres_list = item.get("genres")
            genres_json = json.dumps(genres_list) if isinstance(genres_list, list) else (existing["genres_json"] if existing else "[]")
            poster = str(item.get("poster") or (existing["poster"] if existing else "") or "")
            plot = str(item.get("plot") or (existing["plot"] if existing else "") or "")

            if tmdb_id and TMDB_API_KEY and (not poster or not director or not runtime or not plot):
                meta = fetch_tmdb_metadata(tmdb_id)
                if meta:
                    poster = poster or meta.get("poster", "")
                    director = director or meta.get("director", "")
                    runtime = runtime or meta.get("runtime", "")
                    plot = plot or meta.get("plot", "")
                    if year == "0000" and meta.get("year"):
                        year = meta["year"]
                    if genres_json == "[]" and meta.get("genres"):
                        genres_json = json.dumps(meta["genres"])

            breakdown = compute_enthusiasm_score(title, review_text, sources)
            primary_source = sources[0] if sources else {"label": "Kojima Review", "url": ""}
            score_breakdown_str = (
                f"Total: {breakdown.total_score}/100 "
                f"(Words: {breakdown.word_count_pts}/30, "
                f"Sentiment: {breakdown.sentiment_pts}/35, "
                f"Prestige: {breakdown.prestige_pts}/25, "
                f"Multi-Source: {breakdown.recurrence_pts}/10)"
            )
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            conn.execute(
                """
                INSERT INTO recommendations (
                    slug, title, year, tmdb_id, director, runtime, genres_json, poster, plot,
                    review, sources_json, content_hash,
                    kojima_score, kojima_tier, word_count_pts, sentiment_pts,
                    prestige_pts, recurrence_pts, is_dismissal, sentiment_summary,
                    score_breakdown, primary_url, primary_source, recommended_at, llm_provider, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    title = excluded.title,
                    year = excluded.year,
                    tmdb_id = excluded.tmdb_id,
                    director = excluded.director,
                    runtime = excluded.runtime,
                    genres_json = excluded.genres_json,
                    poster = excluded.poster,
                    plot = excluded.plot,
                    review = excluded.review,
                    sources_json = excluded.sources_json,
                    content_hash = excluded.content_hash,
                    kojima_score = excluded.kojima_score,
                    kojima_tier = excluded.kojima_tier,
                    word_count_pts = excluded.word_count_pts,
                    sentiment_pts = excluded.sentiment_pts,
                    prestige_pts = excluded.prestige_pts,
                    recurrence_pts = excluded.recurrence_pts,
                    is_dismissal = excluded.is_dismissal,
                    sentiment_summary = excluded.sentiment_summary,
                    score_breakdown = excluded.score_breakdown,
                    primary_url = excluded.primary_url,
                    primary_source = excluded.primary_source,
                    recommended_at = excluded.recommended_at,
                    llm_provider = excluded.llm_provider,
                    updated_at = excluded.updated_at
                """,
                (
                    slug,
                    title,
                    year,
                    tmdb_id,
                    director,
                    runtime,
                    genres_json,
                    poster,
                    plot,
                    review_text,
                    json.dumps(sources),
                    content_hash,
                    breakdown.total_score,
                    breakdown.tier,
                    breakdown.word_count_pts,
                    breakdown.sentiment_pts,
                    breakdown.prestige_pts,
                    breakdown.recurrence_pts,
                    "yes" if breakdown.is_dismissal else "no",
                    breakdown.sentiment_summary,
                    score_breakdown_str,
                    primary_source.get("url", ""),
                    primary_source.get("label", ""),
                    recommended_at,
                    LLM_PROVIDER,
                    now_iso,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM recommendations WHERE slug = ?", (slug,)).fetchone()
            return _row_to_feed_dict(row)
        finally:
            conn.close()


def _row_to_feed_dict(row: sqlite3.Row) -> Dict[str, Any]:
    review_text = row["review"]
    try:
        genres = json.loads(row["genres_json"] or "[]")
    except Exception:
        genres = []
    rec_date = (row["recommended_at"] if "recommended_at" in row.keys() else "") or ""
    if not rec_date and row["title"] in MOVIE_RECOMMENDED_DATES:
        rec_date = MOVIE_RECOMMENDED_DATES[row["title"]]
    return {
        "title": row["title"],
        "year": row["year"],
        "tmdb_id": str(row["tmdb_id"]),
        "director": row["director"] or "",
        "runtime": row["runtime"] or "",
        "genres": genres,
        "poster": row["poster"] or "",
        "plot": row["plot"] or "",
        "kojima_score": int(row["kojima_score"]),
        "kojima_tier": row["kojima_tier"],
        "kojima_review": review_text,
        "kojima_word_count": len(review_text.strip().split()),
        "kojima_is_dismissal": row["is_dismissal"],
        "kojima_sentiment_summary": row["sentiment_summary"],
        "kojima_score_breakdown": row["score_breakdown"],
        "kojima_primary_url": row["primary_url"],
        "kojima_primary_source": row["primary_source"],
        "kojima_recommended_at": rec_date,
        "kojima_sources_json": row["sources_json"],
    }


# ---------------------------------------------------------------------------
# Automated RSS / X Feed Scraper
# ---------------------------------------------------------------------------
def _normalize_date_str(raw_date: str) -> str:
    raw = (raw_date or "").strip()
    if not raw:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


LETTERBOXD_NS = "{https://letterboxd.com}"
TMDB_NS = "{https://themoviedb.org}"
X_STATUS_URL_RE = re.compile(
    r"https?://(?:x|twitter)\.com/HIDEO_KOJIMA_EN/status/(?P<id>\d{15,22})",
    re.IGNORECASE,
)
POSTER_IMG_SRC_RE = re.compile(
    r"""<img[^>]+src=['"](?P<src>https?://[^'"]+)['"]""",
    re.IGNORECASE,
)


def _clean_rss_description(raw_desc: str) -> str:
    """
    Strips HTML tags, embedded Letterboxd poster <img>, and trailing X/Twitter status URLs
    or QRT footers so only the verbatim review text remains.
    """
    cleaned = re.sub(r"<img[^>]*>", " ", raw_desc, flags=re.IGNORECASE)
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    # Remove QRT footer blocks and bare x.com/twitter.com status links
    cleaned = re.sub(r"-{4,}\s*QRT\s+on:.*$", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(
        r"(?:https?://)?(?:www\.)?(?:x|twitter)\.com/HIDEO_KOJIMA_EN/status/\d+\S*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def parse_rss_or_atom_xml(xml_text: str) -> List[Dict[str, str]]:
    """
    Parses RSS 2.0 (<item>) or Atom (<entry>) XML into a list of
    {guid, title, text, url, date, explicit_title, explicit_year, explicit_tmdb_id, poster}.
    Supports Letterboxd (<letterboxd:filmTitle>, <letterboxd:filmYear>, <letterboxd:watchedDate>, <tmdb:movieId>).
    """
    entries: List[Dict[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return entries

    # RSS 2.0 <item>
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = _normalize_date_str(item.findtext("pubDate") or "")
        watched_date = _normalize_date_str(item.findtext(f"{LETTERBOXD_NS}watchedDate") or "")
        lb_title = (item.findtext(f"{LETTERBOXD_NS}filmTitle") or "").strip()
        lb_year = (item.findtext(f"{LETTERBOXD_NS}filmYear") or "").strip()
        lb_tmdb_id = (item.findtext(f"{TMDB_NS}movieId") or "").strip()

        # Skip non-movie Letterboxd list items (e.g. /list/best-movies-2025...)
        if "letterboxd.com/" in link and "/list/" in link and not lb_title:
            continue

        poster_match = POSTER_IMG_SRC_RE.search(desc)
        poster = poster_match.group("src").split("?")[0] if poster_match else ""

        x_match = X_STATUS_URL_RE.search(desc) or X_STATUS_URL_RE.search(link)
        canonical_url = (
            f"https://x.com/HIDEO_KOJIMA_EN/status/{x_match.group('id')}"
            if x_match
            else link
        )

        cleaned_desc = _clean_rss_description(desc)
        if cleaned_desc in ("{No caption}", ""):
            cleaned_desc = ""

        guid = (item.findtext("guid") or canonical_url or link or title).strip()
        combined = cleaned_desc if (cleaned_desc and (lb_title or len(cleaned_desc) >= len(title))) else title
        if combined:
            entries.append({
                "guid": guid,
                "title": title,
                "text": combined,
                "url": canonical_url,
                "date": watched_date or pub_date,
                "explicit_title": lb_title,
                "explicit_year": lb_year,
                "explicit_tmdb_id": lb_tmdb_id,
                "poster": poster,
            })

    # Atom <entry>
    atom_ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.findall(f".//{atom_ns}entry") + root.findall(".//entry"):
        title = (entry.findtext(f"{atom_ns}title") or entry.findtext("title") or "").strip()
        summary = (
            entry.findtext(f"{atom_ns}content")
            or entry.findtext(f"{atom_ns}summary")
            or entry.findtext("content")
            or entry.findtext("summary")
            or ""
        ).strip()
        link_el = entry.find(f"{atom_ns}link") or entry.find("link")
        link = ""
        if link_el is not None:
            link = (link_el.attrib.get("href") or link_el.text or "").strip()
        pub_date = _normalize_date_str(
            entry.findtext(f"{atom_ns}published")
            or entry.findtext(f"{atom_ns}updated")
            or entry.findtext("published")
            or entry.findtext("updated")
            or ""
        )
        guid = (entry.findtext(f"{atom_ns}id") or entry.findtext("id") or link or title).strip()
        combined = f"{title}. {summary}".strip(". ") if summary and title not in summary else (summary or title)
        if combined:
            entries.append({
                "guid": guid,
                "title": title,
                "text": combined,
                "url": link,
                "date": pub_date,
                "explicit_title": "",
                "explicit_year": "",
                "explicit_tmdb_id": "",
                "poster": "",
            })

    return entries


def fetch_fxtwitter_post(status_url_or_id: str) -> Optional[Dict[str, str]]:
    """
    Fetches verbatim tweet text and timestamp for a @HIDEO_KOJIMA_EN status via api.fxtwitter.com.
    """
    raw = (status_url_or_id or "").strip()
    m = re.search(r"(?:status/|^(?P<bare>\d{15,22})$)(?P<id>\d{15,22})?", raw)
    status_id = (m.group("bare") or m.group("id")) if m else ""
    if not status_id:
        m2 = X_STATUS_URL_RE.search(raw)
        status_id = m2.group("id") if m2 else ""
    if not status_id:
        return None

    api_url = f"https://api.fxtwitter.com/HIDEO_KOJIMA_EN/status/{status_id}"
    try:
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": "KojimaMovieTracker/1.0 (+https://github.com/mblackman)"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tweet = data.get("tweet") or {}
        text = str(tweet.get("text") or "").strip()
        if not text:
            return None
        created_at = _normalize_date_str(str(tweet.get("created_at") or ""))
        return {
            "id": status_id,
            "text": text,
            "date": created_at,
            "url": f"https://x.com/HIDEO_KOJIMA_EN/status/{status_id}",
        }
    except Exception as exc:
        print(f"[warn] fxtwitter lookup failed for {status_id}: {exc}")
        return None


def ingest_raw_post(
    raw_text: str,
    source_url: str = "",
    source_label: str = "X (@HIDEO_KOJIMA_EN)",
    source_type: str = "x_review",
    explicit_title: Optional[str] = None,
    explicit_year: Optional[str] = None,
    explicit_tmdb_id: Optional[str] = None,
    poster: Optional[str] = None,
    recommended_at: Optional[str] = None,
    guid: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Extracts a movie recommendation from a raw post (or explicit fields), resolves TMDB ID,
    computes its Kojima Enthusiasm Score, and stores it in SQLite.
    """
    init_db(db_path)
    post_guid = guid or hashlib.sha256(f"{source_url}|{raw_text}".encode("utf-8")).hexdigest()

    if explicit_title:
        title = _clean_extracted_title(explicit_title)
        year = (explicit_year or "0000").strip()
        review = _clean_rss_description(raw_text) or raw_text.strip()
    else:
        extracted = extract_movie_from_post(raw_text)
        if not extracted:
            return None
        title, year, review = extracted

    tmdb_id = explicit_tmdb_id or resolve_tmdb_id(title, year) or ""
    rec_date = _normalize_date_str(recommended_at or "") or time.strftime("%Y-%m-%d", time.gmtime())
    record = upsert_recommendation(
        {
            "title": title,
            "year": year,
            "tmdb_id": tmdb_id,
            "poster": poster or "",
            "review": review,
            "recommended_at": rec_date,
            "sources": [
                {
                    "label": source_label,
                    "type": source_type,
                    "url": source_url,
                    "date": rec_date,
                }
            ],
        },
        db_path=db_path,
    )

    with _DB_LOCK:
        conn = get_db_connection(db_path)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO scrape_log (guid, feed_url, raw_text, extracted_title, tmdb_id, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    post_guid,
                    source_url,
                    raw_text,
                    title,
                    record.get("tmdb_id", ""),
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    return record


def discover_kojima_x_reviews_from_news(
    news_rss_urls: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    max_articles_per_feed: int = 12,
) -> List[Dict[str, Any]]:
    """
    Discovers new @HIDEO_KOJIMA_EN movie review statuses cited in entertainment press RSS feeds
    (Google News / Bing News), resolves the verbatim tweet via api.fxtwitter.com, and ingests it.
    """
    init_db(db_path)
    urls = news_rss_urls if news_rss_urls is not None else DEFAULT_KOJIMA_NEWS_RSS_URLS
    ingested: List[Dict[str, Any]] = []

    for feed_url in urls:
        try:
            req = urllib.request.Request(
                feed_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; KojimaMovieTracker/1.0)"},
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                xml_text = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[warn] News RSS discovery fetch failed for {feed_url}: {exc}")
            continue

        entries = parse_rss_or_atom_xml(xml_text)
        for entry in entries[:max_articles_per_feed]:
            article_url = entry.get("url") or ""
            if not article_url:
                continue
            article_guid = f"news-article::{article_url}"
            with _DB_LOCK:
                conn = get_db_connection(db_path)
                try:
                    seen = conn.execute(
                        "SELECT 1 FROM scrape_log WHERE guid = ?", (article_guid,)
                    ).fetchone()
                finally:
                    conn.close()
            if seen:
                continue

            status_ids = set(X_STATUS_URL_RE.findall(entry.get("text") or ""))
            if not status_ids and article_url.startswith(("http://", "https://")):
                try:
                    art_req = urllib.request.Request(
                        article_url,
                        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                    )
                    with urllib.request.urlopen(art_req, timeout=10) as art_resp:
                        html_body = art_resp.read().decode("utf-8", errors="replace")
                    status_ids.update(X_STATUS_URL_RE.findall(html_body))
                except Exception:
                    pass

            for sid in status_ids:
                status_guid = f"x-status::{sid}"
                with _DB_LOCK:
                    conn = get_db_connection(db_path)
                    try:
                        seen_status = conn.execute(
                            "SELECT 1 FROM scrape_log WHERE guid = ? OR feed_url = ?",
                            (status_guid, f"https://x.com/HIDEO_KOJIMA_EN/status/{sid}"),
                        ).fetchone()
                    finally:
                        conn.close()
                if seen_status:
                    continue

                tweet = fetch_fxtwitter_post(sid)
                if not tweet:
                    continue
                rec = ingest_raw_post(
                    raw_text=tweet["text"],
                    source_url=tweet["url"],
                    source_label="X (@HIDEO_KOJIMA_EN)",
                    source_type="x_review",
                    recommended_at=tweet["date"],
                    guid=status_guid,
                    db_path=db_path,
                )
                if rec:
                    ingested.append(rec)

            with _DB_LOCK:
                conn = get_db_connection(db_path)
                try:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO scrape_log (guid, feed_url, raw_text, extracted_title, tmdb_id, scraped_at)
                        VALUES (?, ?, ?, '', '', ?)
                        """,
                        (
                            article_guid,
                            article_url,
                            entry.get("title") or "",
                            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

    return ingested


def poll_rss_feeds(
    feed_urls: Optional[List[str]] = None,
    db_path: Optional[str] = None,
    since: Optional[str] = None,
    discover_x_from_news: bool = False,
) -> List[Dict[str, Any]]:
    """
    Polls configured X/Nitter/Letterboxd RSS feeds, skips already-scraped GUIDs
    (and posts dated before `since`, an ISO date/datetime), and ingests any newly
    detected Kojima movie reviews into SQLite.
    """
    global _LAST_SCRAPE_AT
    init_db(db_path)
    urls = feed_urls if feed_urls is not None else KOJIMA_RSS_URLS
    newly_ingested: List[Dict[str, Any]] = []
    since_date = (since or "")[:10]

    for url in urls:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; KojimaMovieTracker/1.0; +https://github.com/mblackman)"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                xml_text = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[warn] Failed to fetch RSS feed {url}: {exc}")
            continue

        entries = parse_rss_or_atom_xml(xml_text)
        for entry in entries:
            if since_date and entry.get("date") and entry["date"] < since_date:
                continue
            guid = entry["guid"]
            entry_url = entry.get("url") or url
            with _DB_LOCK:
                conn = get_db_connection(db_path)
                try:
                    seen = conn.execute(
                        "SELECT 1 FROM scrape_log WHERE guid = ? OR (feed_url != '' AND feed_url = ?)",
                        (guid, entry_url),
                    ).fetchone()
                finally:
                    conn.close()
            if seen:
                continue

            ingested = ingest_raw_post(
                raw_text=entry["text"],
                source_url=entry_url,
                source_label="X (@HIDEO_KOJIMA_EN)" if "x.com/" in entry_url else "X (@HIDEO_KOJIMA_EN) RSS",
                source_type="x_review",
                explicit_title=entry.get("explicit_title") or None,
                explicit_year=entry.get("explicit_year") or None,
                explicit_tmdb_id=entry.get("explicit_tmdb_id") or None,
                poster=entry.get("poster") or None,
                recommended_at=entry.get("date", ""),
                guid=guid,
                db_path=db_path,
            )
            if ingested:
                newly_ingested.append(ingested)

    if (
        discover_x_from_news
        and feed_urls is None
        and KOJIMA_DISCOVER_X_REVIEWS
        and any(u.startswith(("http://", "https://")) for u in urls)
    ):
        newly_ingested.extend(discover_kojima_x_reviews_from_news(db_path=db_path))

    _LAST_SCRAPE_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return newly_ingested


# ---------------------------------------------------------------------------
# Build Enriched Recommendations Payload for TiddlyWiki Media Importer
# ---------------------------------------------------------------------------
def build_recommendations_feed(
    force_refresh: bool = False,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    init_db(db_path)
    for item in load_catalog():
        upsert_recommendation(item, force_rescore=force_refresh, db_path=db_path)

    with _DB_LOCK:
        conn = get_db_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM recommendations ORDER BY kojima_score DESC, title ASC"
            ).fetchall()
            return [_row_to_feed_dict(r) for r in rows]
        finally:
            conn.close()


CATALOG_PATH = os.environ.get(
    "KOJIMA_CATALOG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog", "kojima-canon.jsonl"),
)
WIKIPEDIA_HEADERS = {
    "User-Agent": "KojimaMovieTracker/1.0 (+https://github.com/mblackman/tiddlywiki-kojima-media)"
}


def load_catalog(catalog_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Returns SEED_RECOMMENDATIONS plus every entry in the JSONL catalog
    (`catalog/kojima-canon.jsonl`) that is not already covered by the seed list.
    """
    items: List[Dict[str, Any]] = list(SEED_RECOMMENDATIONS)
    seen_slugs = {make_slug(str(i["title"]), str(i.get("year") or "0000")) for i in items}
    seed_titles = {str(i["title"]).strip().lower() for i in items}
    path = catalog_path or CATALOG_PATH
    if not path or not os.path.exists(path):
        return items
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"[warn] Skipping invalid catalog line {line_no}: {exc}")
                continue
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            slug = make_slug(title, str(entry.get("year") or "0000"))
            if slug in seen_slugs or title.lower() in seed_titles:
                continue
            seen_slugs.add(slug)
            items.append(entry)
    return items


def fetch_wikipedia_metadata(title: str, year: Optional[str] = None) -> Dict[str, str]:
    """
    Keyless poster/plot enrichment via the Wikipedia search + REST summary APIs.
    Rejects matches whose description is not a film or does not mention the release year.
    """
    query = f"{title} {year} film" if year and year != "0000" else f"{title} film"
    search_url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query, "srlimit": "1", "format": "json"}
    )
    try:
        req = urllib.request.Request(search_url, headers=WIKIPEDIA_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = json.loads(resp.read().decode("utf-8")).get("query", {}).get("search", [])
        if not results:
            return {}
        page = str(results[0]["title"]).replace(" ", "_")
        summary_url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(page, safe="")
        req = urllib.request.Request(summary_url, headers=WIKIPEDIA_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            summary = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"[warn] Wikipedia lookup failed for {title}: {exc}")
        return {}

    description = str(summary.get("description") or "").lower()
    extract = str(summary.get("extract") or "")
    if "film" not in description and "movie" not in description:
        return {}
    if year and year != "0000" and year not in description and year not in extract:
        return {}
    poster = str((summary.get("thumbnail") or {}).get("source") or "").split("?")[0]
    return {"poster": poster, "plot": extract}


def _row_to_ledger_dict(row: sqlite3.Row) -> Dict[str, Any]:
    entry = _row_to_feed_dict(row)
    entry["content_hash"] = row["content_hash"]
    return entry


def _ledger_entry_to_item(prev: Dict[str, Any]) -> Dict[str, Any]:
    try:
        sources = json.loads(prev.get("kojima_sources_json") or "[]")
    except Exception:
        sources = prev.get("sources") or []
    return {
        "title": str(prev.get("title") or "").strip(),
        "year": str(prev.get("year") or "0000").strip(),
        "tmdb_id": str(prev.get("tmdb_id") or ""),
        "director": str(prev.get("director") or ""),
        "runtime": str(prev.get("runtime") or ""),
        "genres": prev.get("genres") if isinstance(prev.get("genres"), list) else [],
        "poster": str(prev.get("poster") or ""),
        "plot": str(prev.get("plot") or ""),
        "review": str(prev.get("kojima_review") or prev.get("review") or ""),
        "recommended_at": str(prev.get("kojima_recommended_at") or prev.get("recommended_at") or ""),
        "sources": sources,
    }


def _restore_ledger_entry(prev: Dict[str, Any], db_path: Optional[str]) -> None:
    """Inserts a persisted ledger entry verbatim (no re-scoring, no network calls)."""
    title = str(prev.get("title") or "").strip()
    if not title:
        return
    year = str(prev.get("year") or "0000").strip()
    genres = prev.get("genres") if isinstance(prev.get("genres"), list) else []
    with _DB_LOCK:
        conn = get_db_connection(db_path)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO recommendations (
                    slug, title, year, tmdb_id, director, runtime, genres_json, poster, plot,
                    review, sources_json, content_hash,
                    kojima_score, kojima_tier, word_count_pts, sentiment_pts,
                    prestige_pts, recurrence_pts, is_dismissal, sentiment_summary,
                    score_breakdown, primary_url, primary_source, recommended_at, llm_provider, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    make_slug(title, year),
                    title,
                    year,
                    str(prev.get("tmdb_id") or ""),
                    str(prev.get("director") or ""),
                    str(prev.get("runtime") or ""),
                    json.dumps(genres),
                    str(prev.get("poster") or ""),
                    str(prev.get("plot") or ""),
                    str(prev.get("kojima_review") or ""),
                    str(prev.get("kojima_sources_json") or "[]"),
                    str(prev.get("content_hash") or ""),
                    int(prev.get("kojima_score") or 0),
                    str(prev.get("kojima_tier") or ""),
                    str(prev.get("kojima_is_dismissal") or "no"),
                    str(prev.get("kojima_sentiment_summary") or ""),
                    str(prev.get("kojima_score_breakdown") or ""),
                    str(prev.get("kojima_primary_url") or ""),
                    str(prev.get("kojima_primary_source") or ""),
                    str(prev.get("kojima_recommended_at") or ""),
                    LLM_PROVIDER,
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def load_ledger(seed_json_path: str) -> Dict[str, Any]:
    """Reads the persisted ledger. Accepts the legacy bare-array format."""
    if not seed_json_path or not os.path.exists(seed_json_path):
        return {"last_updated": None, "items": []}
    try:
        with open(seed_json_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        print(f"[warn] Could not read ledger {seed_json_path}: {exc}")
        return {"last_updated": None, "items": []}
    if isinstance(data, list):
        return {"last_updated": None, "items": data}
    return {"last_updated": data.get("last_updated"), "items": data.get("items") or []}


def sync_static_files(
    seed_json_path: str = "demo/seed-recommendations.json",
    api_json_path: str = "build/api/kojima-recommendations.json",
    scrape_rss: bool = True,
    force_refresh: bool = False,
    enrich: bool = True,
    db_path: Optional[str] = None,
    catalog_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Incremental serverless / GitHub Pages generator backed by a Git-persisted ledger:
      1. Restores every entry from `seed_json_path` verbatim (scores, posters, TMDB metadata),
         so unchanged movies are never re-scored or re-fetched.
      2. Upserts the catalog (SEED_RECOMMENDATIONS + catalog/kojima-canon.jsonl); only new or
         edited entries (content_hash mismatch) are scored. Missing posters are enriched
         from Wikipedia (keyless) and TMDB (when TMDB_API_KEY is set).
      3. Polls KOJIMA_RSS_URLS, skipping posts dated before the ledger's `last_updated`.
      4. Writes the ledger (bumping `last_updated` only when items changed) and the static API payload.
    """
    temp_dir = None
    if db_path is None:
        temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(temp_dir.name, "kojima_static.db")
    try:
        init_db(db_path)
        ledger = load_ledger(seed_json_path)
        previous_items = ledger["items"]
        catalog = load_catalog(catalog_path)
        catalog_slugs = {
            make_slug(str(i["title"]), str(i.get("year") or "0000")) for i in catalog
        }

        for prev in previous_items:
            title = str(prev.get("title") or "").strip()
            if not title:
                continue
            slug = make_slug(title, str(prev.get("year") or "0000"))
            if force_refresh and slug not in catalog_slugs:
                upsert_recommendation(_ledger_entry_to_item(prev), force_rescore=True, db_path=db_path)
            else:
                _restore_ledger_entry(prev, db_path)

        enriched = 0
        for raw_item in catalog:
            item = dict(raw_item)
            if enrich and not item.get("poster"):
                slug = make_slug(str(item["title"]), str(item.get("year") or "0000"))
                conn = get_db_connection(db_path)
                try:
                    row = conn.execute(
                        "SELECT poster FROM recommendations WHERE slug = ?", (slug,)
                    ).fetchone()
                finally:
                    conn.close()
                if not (row and row["poster"]):
                    meta = fetch_wikipedia_metadata(str(item["title"]), str(item.get("year") or ""))
                    if meta.get("poster"):
                        item["poster"] = meta["poster"]
                        item["plot"] = item.get("plot") or meta.get("plot", "")
                        enriched += 1
                    time.sleep(0.1)
            upsert_recommendation(item, force_rescore=force_refresh, db_path=db_path)

        ingested: List[Dict[str, Any]] = []
        if scrape_rss and KOJIMA_RSS_URLS:
            with _DB_LOCK:
                conn = get_db_connection(db_path)
                try:
                    existing_rows = conn.execute(
                        "SELECT title, tmdb_id, primary_url, sources_json FROM recommendations"
                    ).fetchall()
                    now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    for erow in existing_rows:
                        urls_to_mark = set()
                        if erow["primary_url"]:
                            raw_u = str(erow["primary_url"]).strip()
                            urls_to_mark.add(raw_u)
                            urls_to_mark.add(raw_u.rstrip("/"))
                        try:
                            for s in json.loads(erow["sources_json"] or "[]"):
                                if s.get("url"):
                                    raw_u = str(s["url"]).strip()
                                    urls_to_mark.add(raw_u)
                                    urls_to_mark.add(raw_u.rstrip("/"))
                        except Exception:
                            pass
                        for u in urls_to_mark:
                            conn.execute(
                                "INSERT OR IGNORE INTO scrape_log (guid, feed_url, raw_text, extracted_title, tmdb_id, scraped_at) VALUES (?, ?, '', ?, ?, ?)",
                                (u, u, erow["title"], erow["tmdb_id"], now_str),
                            )
                            xm = X_STATUS_URL_RE.search(u)
                            if xm:
                                conn.execute(
                                    "INSERT OR IGNORE INTO scrape_log (guid, feed_url, raw_text, extracted_title, tmdb_id, scraped_at) VALUES (?, ?, '', ?, ?, ?)",
                                    (f"x-status::{xm.group('id')}", u, erow["title"], erow["tmdb_id"], now_str),
                                )
                    conn.commit()
                finally:
                    conn.close()
            ingested = poll_rss_feeds(db_path=db_path, discover_x_from_news=True)

        with _DB_LOCK:
            conn = get_db_connection(db_path)
            try:
                rows = conn.execute(
                    "SELECT * FROM recommendations ORDER BY kojima_score DESC, title ASC"
                ).fetchall()
                ledger_items = [_row_to_ledger_dict(r) for r in rows]
            finally:
                conn.close()
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    changed = ledger_items != previous_items
    last_updated = now_iso if (changed or not ledger.get("last_updated")) else ledger["last_updated"]

    if seed_json_path:
        os.makedirs(os.path.dirname(os.path.abspath(seed_json_path)), exist_ok=True)
        with open(seed_json_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "last_updated": last_updated,
                    "scoring_version": SCORING_ENGINE_VERSION,
                    "count": len(ledger_items),
                    "items": ledger_items,
                },
                fh,
                indent=2,
                ensure_ascii=False,
            )
            fh.write("\n")

    feed = [{k: v for k, v in item.items() if k != "content_hash"} for item in ledger_items]
    payload = {
        "generated_at": now_iso,
        "last_updated": last_updated,
        "scoring_version": SCORING_ENGINE_VERSION,
        "llm_provider": LLM_PROVIDER,
        "newly_ingested_count": len(ingested),
        "enriched_count": enriched,
        "changed": changed,
        "count": len(feed),
        "items": feed,
    }

    if api_json_path:
        abs_api_path = os.path.abspath(api_json_path)
        os.makedirs(os.path.dirname(abs_api_path), exist_ok=True)
        payload_str = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        with open(abs_api_path, "w", encoding="utf-8") as fh:
            fh.write(payload_str)
        if abs_api_path.endswith(".json"):
            no_ext_path = abs_api_path[:-5]
            if not os.path.isdir(no_ext_path):
                with open(no_ext_path, "w", encoding="utf-8") as fh:
                    fh.write(payload_str)

    return payload


# ---------------------------------------------------------------------------
# HTTP Server with CORS for Browser TiddlyWiki Sync (`tm-sync-kojima`)
# ---------------------------------------------------------------------------
class KojimaSyncHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self, status: int = 200, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_OPTIONS(self) -> None:
        self._send_cors_headers(204)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path.startswith("/api/kojima-recommendations"):
            force_refresh = query.get("refresh", ["0"])[0] in ("1", "true", "yes")
            hide_dismissals = query.get("hide_dismissals", ["0"])[0] in ("1", "true", "yes")
            feed = build_recommendations_feed(force_refresh=force_refresh)
            if hide_dismissals:
                feed = [item for item in feed if item.get("kojima_is_dismissal") != "yes"]

            payload = {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "last_scrape_at": _LAST_SCRAPE_AT,
                "llm_provider": LLM_PROVIDER,
                "count": len(feed),
                "items": feed,
            }
            self._send_cors_headers(200)
            self.wfile.write(json.dumps(payload, indent=2).encode("utf-8"))
        elif parsed.path.startswith("/health"):
            feed = build_recommendations_feed(force_refresh=False)
            self._send_cors_headers(200)
            self.wfile.write(
                json.dumps({
                    "status": "ok",
                    "count": len(feed),
                    "db_path": KOJIMA_DB_PATH,
                    "llm_provider": LLM_PROVIDER,
                    "last_scrape_at": _LAST_SCRAPE_AT,
                }).encode("utf-8")
            )
        else:
            self._send_cors_headers(404)
            self.wfile.write(json.dumps({"error": "Not found"}).encode("utf-8"))

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        content_len = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            body = json.loads(raw_body) if raw_body else {}
        except Exception:
            body = {}

        if parsed.path.startswith("/api/scrape"):
            urls = body.get("rss_urls") or KOJIMA_RSS_URLS
            ingested = poll_rss_feeds(feed_urls=urls)
            self._send_cors_headers(200)
            self.wfile.write(
                json.dumps({
                    "status": "ok",
                    "ingested_count": len(ingested),
                    "ingested": ingested,
                }, indent=2).encode("utf-8")
            )
        elif parsed.path.startswith("/api/ingest-post"):
            text = str(body.get("text") or body.get("review") or "").strip()
            if not text:
                self._send_cors_headers(400)
                self.wfile.write(json.dumps({"error": "Missing 'text' field"}).encode("utf-8"))
                return
            record = ingest_raw_post(
                raw_text=text,
                source_url=str(body.get("url") or ""),
                source_label=str(body.get("source_label") or "X (@HIDEO_KOJIMA_EN)"),
                source_type=str(body.get("source_type") or "x_review"),
                explicit_title=body.get("title"),
                explicit_year=body.get("year"),
                explicit_tmdb_id=body.get("tmdb_id"),
            )
            if not record:
                self._send_cors_headers(422)
                self.wfile.write(json.dumps({"error": "Could not extract a movie title from post text"}).encode("utf-8"))
                return
            self._send_cors_headers(200)
            self.wfile.write(json.dumps({"status": "ok", "item": record}, indent=2).encode("utf-8"))
        else:
            self._send_cors_headers(404)
            self.wfile.write(json.dumps({"error": "Not found"}).encode("utf-8"))


def _background_scraper_loop(interval_seconds: int) -> None:
    while True:
        try:
            if KOJIMA_RSS_URLS:
                ingested = poll_rss_feeds()
                if ingested:
                    print(f"[scraper] Ingested {len(ingested)} new Kojima movie post(s).")
        except Exception as exc:
            print(f"[warn] Background scraper error: {exc}")
        time.sleep(max(60, interval_seconds))


if __name__ == "__main__":
    import sys

    if "--sync-static" in sys.argv:
        seed_out = os.environ.get("KOJIMA_SEED_JSON_PATH", "demo/seed-recommendations.json")
        api_out = os.environ.get("KOJIMA_API_JSON_PATH", "build/api/kojima-recommendations.json")
        payload = sync_static_files(
            seed_json_path=seed_out,
            api_json_path=api_out,
            scrape_rss=True,
            force_refresh="--force-rescore" in sys.argv,
            enrich="--no-enrich" not in sys.argv and os.environ.get("KOJIMA_ENRICH", "1") != "0",
        )
        print(
            f"✅ {payload['count']} Kojima recommendations "
            f"(+{payload['newly_ingested_count']} from RSS, {payload['enriched_count']} posters enriched, "
            f"{'changed' if payload['changed'] else 'unchanged'}; last_updated={payload['last_updated']}) "
            f"-> {seed_out} & {api_out}"
        )
        sys.exit(0)

    if "--dump-json" in sys.argv:
        print(json.dumps(build_recommendations_feed(), indent=2))
        sys.exit(0)

    if "--scrape-once" in sys.argv:
        ingested = poll_rss_feeds()
        print(json.dumps({"ingested_count": len(ingested), "items": ingested}, indent=2))
        sys.exit(0)

    build_recommendations_feed()
    if SCRAPE_INTERVAL_SECONDS > 0:
        t = threading.Thread(
            target=_background_scraper_loop,
            args=(SCRAPE_INTERVAL_SECONDS,),
            daemon=True,
        )
        t.start()
        print(f"Started background RSS scraper thread (interval={SCRAPE_INTERVAL_SECONDS}s)")

    print(f"Starting Kojima Movie Sync Service on 0.0.0.0:{PORT} (DB: {KOJIMA_DB_PATH}) ...")
    server = HTTPServer(("0.0.0.0", PORT), KojimaSyncHandler)
    server.serve_forever()
