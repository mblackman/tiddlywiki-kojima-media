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
- Persists recommendations, resolved TMDB IDs, and 5-level enthusiasm ratings in SQLite
  (`data/kojima_reviews.db` or `KOJIMA_DB_PATH`) so repeat syncs are fast and deterministic.
- Background RSS/Atom scraper daemon (`KOJIMA_RSS_URLS`, `SCRAPE_INTERVAL_SECONDS`)
  that extracts movie titles from new `@HIDEO_KOJIMA_EN` posts, resolves `tmdb_id`,
  and rates enthusiasm automatically.
- Exposes a CORS-enabled HTTP JSON endpoint (`GET /api/kojima-recommendations`,
  `POST /api/scrape`, `POST /api/ingest-post`, `GET /health`) for one-click syncing
  directly from TiddlyWiki in the browser.
"""

from __future__ import annotations

import hashlib
import html
import json
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
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
PORT = int(os.getenv("PORT", "8765"))

DEFAULT_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
KOJIMA_DB_PATH = os.getenv("KOJIMA_DB_PATH", os.path.join(DEFAULT_DB_DIR, "kojima_reviews.db"))
DEFAULT_KOJIMA_RSS_URLS = [
    "https://letterboxd.com/kojima_hideo/rss/",
]
DEFAULT_KOJIMA_NEWS_RSS_URLS = [
    "https://news.google.com/rss/search?q=%22Hideo+Kojima%22+(movie+OR+film+OR+review+OR+watched)+when:1y&hl=en-US&gl=US&ceid=US:en",
    "https://www.bing.com/news/search?q=%22Hideo+Kojima%22+review+movie&format=rss",
]
DEFAULT_KOJIMA_LIST_NEWS_RSS_URLS = [
    "https://news.google.com/rss/search?q=%22Hideo+Kojima%22+(%22favorite+movies%22+OR+%22best+movies%22+OR+%22top+movies%22+OR+%22favorite+films%22+OR+%22Criterion+Closet%22)+when:1y&hl=en-US&gl=US&ceid=US:en",
    "https://www.bing.com/news/search?q=%22Hideo+Kojima%22+(%22favorite+movies%22+OR+%22best+movies%22+OR+%22top+movies%22)&format=rss",
]
_env_rss_urls = [u.strip() for u in os.getenv("KOJIMA_RSS_URLS", "").split(",") if u.strip()]
KOJIMA_RSS_URLS = _env_rss_urls if _env_rss_urls else list(DEFAULT_KOJIMA_RSS_URLS)
KOJIMA_DISCOVER_X_REVIEWS = os.getenv("KOJIMA_DISCOVER_X_REVIEWS", "1") not in ("0", "false", "no")
SCRAPE_INTERVAL_SECONDS = int(os.getenv("SCRAPE_INTERVAL_SECONDS", "0"))

# ---------------------------------------------------------------------------
# Default Recommendation Dates
# ---------------------------------------------------------------------------
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
# 5-Level Enthusiasm Rating Engine
# ---------------------------------------------------------------------------
ONE_SENTENCE_DISMISSAL_RE = re.compile(
    r"^(?:i\s+)?(?:went\s+to\s+see|saw|watched|finally\s+got\s+to\s+see)\b[^.!?]*[.!?]?$",
    re.IGNORECASE,
)

LEVEL_LABELS: Dict[int, str] = {
    5: "Canon",
    4: "Loved",
    3: "Liked",
    2: "Noted",
    1: "Dismissed",
}

# Curated source type -> minimum level implied by being selected for that list.
SOURCE_TYPE_LEVELS: Dict[str, int] = {
    "gq_top_4": 5,             # GQ All-Time Top 4
    "rt_top_5": 5,             # Rotten Tomatoes Five Favorite Films
    "mgs_dna_core": 5,         # Junker HQ Core 7 films that shaped Metal Gear Solid
    "junkerhq_top_30": 5,      # Junker HQ All-Time Top 30 Personal Favorites
    "criterion_closet": 5,     # Criterion Collection Closet Picks
    "annual_top_list": 4,      # Year-End / Mid-Year Top Movies List
    "junkerhq_runner_up": 4,   # Junker HQ Runners-Up / MGS Influences
}

SOURCE_TYPE_CONTEXT: Dict[str, str] = {
    "gq_top_4": "All-time Top 4 (GQ)",
    "rt_top_5": "Five Favorite Films (Rotten Tomatoes)",
    "mgs_dna_core": "Core Metal Gear Solid influence",
    "junkerhq_top_30": "All-time Top 30 personal favorite",
    "criterion_closet": "Criterion Closet pick",
    "annual_top_list": "Year-end top list selection",
    "junkerhq_runner_up": "Metal Gear Solid influence (runner-up)",
}

ALL_CURATED_TYPES = set(SOURCE_TYPE_LEVELS)

RAVE_PHRASES = [
    "masterpiece", "history of cinema", "miracle", "god of cinema", "first god", "burned into my brain",
    "more than a film", "too good to be true", "so, so good", "speechless", "tearjerker", "must-see",
    "must-watch", "best film", "best films", "best movie", "best movies", "top two", "top three",
    "number one", "number-one", "of the year", "blown away", "should see", "best possible way",
    "tears to my eyes",
]

STRONG_PRAISE_TERMS = [
    "magnificent", "incredible", "amazing", "fantastic", "greatest", "brilliant", "towering", "stunning",
    "superb", "perfect", "awesome", "eye-opener", "no compromise", "completely hooked", "hooked",
    "favorite", "favourite", "cry", "cried", "crying", "bawled", "so great", "too cool",
]

MILD_PRAISE_TERMS = [
    "excellent", "great", "loved", "love it", "enjoyed", "beautiful", "satisfying", "spot-on",
    "impressive", "moving", "fun", "gripping", "terrific", "wonderful", "recommend", "recommended",
    "well made", "well-made", "high-quality", "entertaining", "outstanding", "craftsmanlike",
    "works fairly well", "never lets up",
]

REWATCH_OR_OWNERSHIP_RE = re.compile(
    r"\b(?:re-?watch(?:ed|ing)?|(?:watch|see|saw|seeing)\w* (?:it )?again|(?:second|third|fourth|fifth) time"
    r"|for the (?!first\b)\w+ time|(?:bought|purchased|picked (?:it )?up|i have|i own)\b[^.!?]{0,60}"
    r"\b(?:4k|uhd|blu-?ray|bd|dvd|box set|edition)|4k uhd|box set|special edition)\b",
    re.IGNORECASE,
)

NEGATION_RE = re.compile(r"\b(?:not|never|no|hardly|isn't|wasn't|didn't|don't|doesn't|wouldn't)\W+(?:\w+\W+){0,2}$", re.IGNORECASE)

_TERM_PATTERNS: Dict[str, "re.Pattern[str]"] = {}


def _term_pattern(term: str) -> "re.Pattern[str]":
    pattern = _TERM_PATTERNS.get(term)
    if pattern is None:
        pattern = re.compile(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", re.IGNORECASE)
        _TERM_PATTERNS[term] = pattern
    return pattern


def _count_term_hits(text: str, terms: List[str]) -> List[str]:
    """
    Returns the terms that appear as whole words/phrases and are not negated
    (e.g. 'not a masterpiece'). Shorter terms contained in an already-matched
    longer term (e.g. 'hooked' inside 'completely hooked') are not double counted.
    """
    hits: List[str] = []
    claimed: List[Tuple[int, int]] = []
    for term in sorted(terms, key=len, reverse=True):
        for match in _term_pattern(term).finditer(text):
            span = match.span()
            if any(s <= span[0] and span[1] <= e for s, e in claimed):
                continue
            if NEGATION_RE.search(text[max(0, span[0] - 40):span[0]]):
                continue
            claimed.append(span)
            hits.append(term)
            break
    return hits


@dataclass
class EnthusiasmRating:
    level: int
    label: str
    is_dismissal: bool
    reason: str
    method: str  # "source", "llm", "keywords", or "dismissal"


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


def classify_review_text_keywords(review_text: str) -> Tuple[int, str]:
    """
    Keyword fallback classifier for free-text reviews. Returns (level, reason) with
    level 4 (Loved), 3 (Liked), or 2 (Noted).
    """
    raves = _count_term_hits(review_text, RAVE_PHRASES)
    strong = _count_term_hits(review_text, STRONG_PRAISE_TERMS)
    mild = _count_term_hits(review_text, MILD_PRAISE_TERMS)
    all_praise = raves + strong + mild

    if raves or strong or len(all_praise) >= 3:
        return 4, f"Rave language: {', '.join(all_praise)}."
    if all_praise:
        return 3, f"Positive language: {', '.join(all_praise)}."
    if REWATCH_OR_OWNERSHIP_RE.search(review_text):
        return 3, "Chose to rewatch it or owns a premium edition."
    return 2, "Mentions or discusses the film without clear praise."


def analyze_sentiment_with_llm(
    title: str,
    review_text: str,
    sources: Optional[List[Dict[str, str]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Calls OpenAI, Gemini, or local Ollama to classify a review as loved / liked / noted.
    Returns None if LLM_PROVIDER is 'heuristic' or if the network call fails.
    """
    if LLM_PROVIDER == "heuristic":
        return None

    prompt = (
        f"Classify Hideo Kojima's enthusiasm for the movie '{title}' from his post below.\n"
        f"Post:\n\"\"\"{review_text}\"\"\"\n\n"
        "Choose exactly one level:\n"
        "- \"loved\": a rave - superlatives, strong emotion, calls it among the best, or says it moved him deeply.\n"
        "- \"liked\": clearly positive or recommends it, or he chose to rewatch it / owns a premium edition.\n"
        "- \"noted\": neutral, purely descriptive or analytical, mixed, or only announces that he is watching it.\n"
        "Return strict JSON: {\"level\": \"loved\" | \"liked\" | \"noted\", \"reason\": \"<one short sentence>\"}"
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


def compute_enthusiasm_level(
    title: str,
    review_text: str,
    sources: List[Dict[str, str]],
) -> EnthusiasmRating:
    """
    Rates Kojima's enthusiasm on a 5-level scale:
      5 Canon     - on an all-time list (GQ, Rotten Tomatoes, MGS DNA, Junker HQ Top 30, Criterion Closet)
      4 Loved     - on a year-end top list / MGS runner-up list, or the review text is a rave
      3 Liked     - positive review, or a rewatch / premium-edition ownership signal
      2 Noted     - neutral, analytical, or a viewing announcement
      1 Dismissed - the one-sentence 'I watched X at the theater.' non-review
    The final level is the max of the curated-source level and the text level. Text is only
    classified (LLM when configured, keyword fallback otherwise) when it could raise the level.
    """
    source_types = [s.get("type", "x_review") for s in sources] if sources else ["x_review"]

    if is_one_sentence_dismissal(review_text, source_types):
        return EnthusiasmRating(
            level=1,
            label=LEVEL_LABELS[1],
            is_dismissal=True,
            reason="One-sentence 'I watched it' non-review (no praise commentary).",
            method="dismissal",
        )

    source_level = max((SOURCE_TYPE_LEVELS.get(t, 0) for t in source_types), default=0)
    if source_level >= 4:
        best_type = max(source_types, key=lambda t: SOURCE_TYPE_LEVELS.get(t, 0))
        return EnthusiasmRating(
            level=source_level,
            label=LEVEL_LABELS[source_level],
            is_dismissal=False,
            reason=f"{SOURCE_TYPE_CONTEXT.get(best_type, best_type)}.",
            method="source",
        )

    text_level, reason, method = 0, "", "keywords"
    llm_result = analyze_sentiment_with_llm(title, review_text, sources)
    if isinstance(llm_result, dict):
        llm_level = {"loved": 4, "liked": 3, "noted": 2}.get(str(llm_result.get("level", "")).strip().lower())
        if llm_level:
            text_level, method = llm_level, "llm"
            reason = str(llm_result.get("reason") or "AI-classified review.").strip()
        else:
            print(f"[warn] LLM returned an unrecognised level for {title}: {llm_result!r}; using keyword fallback")
    if not text_level:
        text_level, reason = classify_review_text_keywords(review_text)

    level = max(source_level, text_level)
    return EnthusiasmRating(
        level=level,
        label=LEVEL_LABELS[level],
        is_dismissal=False,
        reason=reason,
        method=method,
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


SCORING_ENGINE_VERSION = "v3.0"

NON_MOVIE_TITLE_RE = re.compile(
    r"\b(?:stranger\s+things\s*\d*|season\s+\d+|episode\s+\d+)\b",
    re.IGNORECASE,
)

GENERIC_LIST_BLURB_RE = re.compile(
    r"^(?:named\s+to|selected\s+(?:by|for)|featured\s+(?:on|in)|included\s+(?:on|in)|picked\s+in)\b",
    re.IGNORECASE,
)


def _is_generic_list_blurb(text: str) -> bool:
    stripped = (text or "").strip()
    return not stripped or bool(GENERIC_LIST_BLURB_RE.match(stripped))


def _find_existing_recommendation(
    conn: sqlite3.Connection,
    title: str,
    year: str,
    tmdb_id: str = "",
) -> Optional[sqlite3.Row]:
    slug = make_slug(title, year)
    row = conn.execute("SELECT * FROM recommendations WHERE slug = ?", (slug,)).fetchone()
    if row is not None:
        return row
    if tmdb_id:
        row = conn.execute(
            "SELECT * FROM recommendations WHERE tmdb_id = ? AND tmdb_id != ''",
            (str(tmdb_id),),
        ).fetchone()
        if row is not None:
            return row
    norm_title = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    candidates = conn.execute(
        "SELECT * FROM recommendations WHERE slug LIKE ?",
        (f"{norm_title}::%",),
    ).fetchall()
    for cand in candidates:
        cand_year = str(cand["year"] or "0000").strip()
        if year == "0000" or cand_year == "0000":
            return cand
        if year.isdigit() and cand_year.isdigit() and abs(int(year) - int(cand_year)) <= 1:
            return cand
    return None


def _merge_sources(
    incoming_sources: List[Dict[str, str]],
    existing_sources: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    combined: List[Dict[str, str]] = []
    seen_keys = set()
    for src in list(incoming_sources) + list(existing_sources):
        if not isinstance(src, dict):
            continue
        url = str(src.get("url") or "").strip().rstrip("/")
        key = f"url::{url}" if url else f"type::{src.get('type', '')}::{src.get('label', '')}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        combined.append(src)
    combined.sort(key=lambda s: SOURCE_TYPE_LEVELS.get(str(s.get("type") or ""), 0), reverse=True)
    return combined


def upsert_recommendation(
    item: Dict[str, Any],
    force_rescore: bool = False,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Computes (or reuses cached) enthusiasm score for a movie item and persists to SQLite.
    When an entry already exists (matched by slug, tmdb_id, or title with unknown/±1 year),
    merges curated/review sources and preserves the longer substantive review text.
    """
    init_db(db_path)
    title = str(item["title"]).strip()
    year = str(item.get("year") or "0000").strip()
    incoming_tmdb_id = str(item.get("tmdb_id") or "").strip()
    slug = make_slug(title, year)
    review_text = str(item.get("review") or "").strip()
    sources: List[Dict[str, str]] = list(item.get("sources") or [])
    recommended_at = _infer_recommended_at(title, item, sources)
    sources_json = json.dumps(sources, sort_keys=True)
    raw_valid_hashes = {
        hashlib.sha256(
            f"{SCORING_ENGINE_VERSION}|{title}|{year}|{review_text}|{sources_json}|{recommended_at}|{prov}".encode("utf-8")
        ).hexdigest()
        for prov in (LLM_PROVIDER, "gemini", "heuristic", "openai")
    }

    with _DB_LOCK:
        conn = get_db_connection(db_path)
        try:
            existing = _find_existing_recommendation(conn, title, year, incoming_tmdb_id)
            old_slug = str(existing["slug"]) if existing else slug

            if existing:
                existing_year = str(existing["year"] or "0000").strip()
                if year == "0000" and existing_year != "0000":
                    year = existing_year
                    slug = old_slug
                elif existing_year == "0000" and year != "0000":
                    slug = make_slug(title, year)
                else:
                    year = existing_year
                    slug = old_slug

                try:
                    old_sources = json.loads(existing["sources_json"] or "[]")
                except Exception:
                    old_sources = []
                sources = _merge_sources(sources, old_sources)

                old_review = str(existing["review"] or "").strip()
                if old_review:
                    if _is_generic_list_blurb(review_text) and not _is_generic_list_blurb(old_review):
                        review_text = old_review
                    elif (
                        not _is_generic_list_blurb(old_review)
                        and not _is_generic_list_blurb(review_text)
                        and len(old_review) > len(review_text)
                    ):
                        review_text = old_review
                    elif not review_text:
                        review_text = old_review

                if existing["recommended_at"] and not item.get("recommended_at") and not item.get("kojima_recommended_at"):
                    recommended_at = str(existing["recommended_at"])
                else:
                    recommended_at = _infer_recommended_at(title, item, sources)

            sources_json = json.dumps(sources, sort_keys=True)
            content_hash = hashlib.sha256(
                f"{SCORING_ENGINE_VERSION}|{title}|{year}|{review_text}|{sources_json}|{recommended_at}|{LLM_PROVIDER}".encode("utf-8")
            ).hexdigest()
            valid_hashes = raw_valid_hashes | {
                hashlib.sha256(
                    f"{SCORING_ENGINE_VERSION}|{title}|{year}|{review_text}|{sources_json}|{recommended_at}|{prov}".encode("utf-8")
                ).hexdigest()
                for prov in (LLM_PROVIDER, "gemini", "heuristic", "openai")
            }

            if (
                existing
                and not force_rescore
                and old_slug == slug
                and existing["content_hash"] in valid_hashes
                and len(sources) == len(old_sources)
            ):
                # Only backfill tmdb_id if explicitly provided on item (avoid repeating network lookups on cache hits)
                if not existing["tmdb_id"] and incoming_tmdb_id:
                    conn.execute(
                        "UPDATE recommendations SET tmdb_id = ? WHERE slug = ?",
                        (incoming_tmdb_id, slug),
                    )
                    conn.commit()
                # Backfill poster/plot/director discovered after the original scoring run
                if not existing["poster"] and item.get("poster"):
                    conn.execute(
                        "UPDATE recommendations SET poster = ?, plot = CASE WHEN plot = '' THEN ? ELSE plot END WHERE slug = ?",
                        (str(item["poster"]), str(item.get("plot") or ""), slug),
                    )
                    conn.commit()
                if not existing["director"] and item.get("director"):
                    conn.execute(
                        "UPDATE recommendations SET director = ? WHERE slug = ?",
                        (str(item["director"]), slug),
                    )
                    conn.commit()
                row = conn.execute("SELECT * FROM recommendations WHERE slug = ?", (slug,)).fetchone()
                return _row_to_feed_dict(row)

            if existing and old_slug != slug:
                conn.execute("DELETE FROM recommendations WHERE slug = ?", (old_slug,))

            tmdb_id = str(
                incoming_tmdb_id
                or (existing["tmdb_id"] if existing else "")
                or (resolve_tmdb_id(title, year) if (not existing or force_rescore) else "")
                or ""
            )
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

            rating = compute_enthusiasm_level(title, review_text, sources)
            primary_source = sources[0] if sources else {"label": "Kojima Review", "url": ""}
            score_breakdown_str = f"{rating.label} ({rating.level}/5) - {rating.reason}"
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
                    rating.level,
                    rating.label,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    "yes" if rating.is_dismissal else "no",
                    rating.reason,
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

    if NON_MOVIE_TITLE_RE.search(title):
        return None

    tmdb_id = explicit_tmdb_id or ""
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


WIKIPEDIA_DIRECTOR_RE = re.compile(
    r"\bdirected by ([A-Z][\w\s.\-’'ñáéíóúüöäëïç,&]+?)(?=\s+(?:and\s+(?:written|produced|starring|based|edited|featuring)|from\s+a|based\s+on|starring\b|with\b|who\b|in\s+his|in\s+her|in\s+their|that\b|which\b)|[.,;()])"
)


def fetch_wikipedia_metadata(title: str, year: Optional[str] = None) -> Dict[str, str]:
    """
    Keyless poster/plot/director enrichment via the Wikipedia search + REST summary APIs.
    Rejects matches whose description is not a film or does not mention the release year.
    """
    query = f"{title} {year} film" if year and year != "0000" else f"{title} film"
    search_url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query, "srlimit": "5", "format": "json"}
    )
    try:
        req = urllib.request.Request(search_url, headers=WIKIPEDIA_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = json.loads(resp.read().decode("utf-8")).get("query", {}).get("search", [])
        if not results:
            return {}
    except Exception as exc:
        print(f"[warn] Wikipedia lookup failed for {title}: {exc}")
        return {}

    norm_t = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()

    def _rank_candidate(cand: Dict[str, Any]) -> int:
        raw_rt = str(cand.get("title") or "")
        base_rt = re.sub(r"\s*\([^)]*(?:film|movie)[^)]*\)\s*$", "", raw_rt, flags=re.IGNORECASE)
        norm_rt = re.sub(r"[^a-z0-9]+", " ", base_rt.lower()).strip()
        if norm_rt == norm_t:
            return 0
        return 1

    for res in sorted(results, key=_rank_candidate):
        raw_rt = str(res.get("title") or "")
        base_rt = re.sub(r"\s*\([^)]*(?:film|movie)[^)]*\)\s*$", "", raw_rt, flags=re.IGNORECASE)
        norm_rt = re.sub(r"[^a-z0-9]+", " ", base_rt.lower()).strip()
        if norm_rt != norm_t:
            continue
        page = raw_rt.replace(" ", "_")
        summary_url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(page, safe="")
        try:
            req = urllib.request.Request(summary_url, headers=WIKIPEDIA_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                summary = json.loads(resp.read().decode("utf-8"))
        except Exception:
            continue

        description = str(summary.get("description") or "").lower()
        extract = str(summary.get("extract") or "")
        if "film" not in description and "movie" not in description and "documentary" not in description:
            continue
        if year and year != "0000":
            valid_years = (
                {str(int(year) - 1), year, str(int(year) + 1)}
                if year.isdigit()
                else {year}
            )
            if not any(vy in description or vy in extract or vy in raw_rt for vy in valid_years):
                continue
        poster = str((summary.get("thumbnail") or {}).get("source") or "").split("?")[0]
        dm = WIKIPEDIA_DIRECTOR_RE.search(extract) or WIKIPEDIA_DIRECTOR_RE.search(str(summary.get("description") or ""))
        director = dm.group(1).strip() if dm else ""
        return {"poster": poster, "plot": extract, "director": director}

    return {}


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
    if not title or NON_MOVIE_TITLE_RE.search(title):
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


def _infer_list_source_type(list_title: str, list_url: str = "") -> Tuple[str, str]:
    combined = f"{list_title} {list_url}".lower()
    clean_title = (list_title or "").strip() or "Hideo Kojima Curated List"
    if "criterion" in combined:
        return "criterion_closet", clean_title
    if "gq" in combined or "top 4" in combined or "top-4" in combined:
        return "gq_top_4", clean_title
    if "rotten tomatoes" in combined or "five favorite" in combined:
        return "rt_top_5", clean_title
    if "top 30" in combined or "top-30" in combined or "junker" in combined:
        return "junkerhq_top_30", clean_title
    return "annual_top_list", clean_title


def extract_letterboxd_lists_from_rss(xml_text: str) -> List[Dict[str, str]]:
    """
    Extracts Letterboxd curated list entries (`<item>` elements without `<letterboxd:filmTitle>`
    whose link or guid references a list, or whose title is a curated list).
    """
    lists: List[Dict[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return lists

    for item in root.findall(".//item"):
        lb_title = (item.findtext(f"{LETTERBOXD_NS}filmTitle") or "").strip()
        if lb_title:
            continue
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link or title).strip()
        pub_date = _normalize_date_str(item.findtext("pubDate") or "")
        is_list_item = (
            "/list/" in link
            or "letterboxd-list-" in guid
            or bool(re.search(r"\b(?:best|top|favorite|favourite)\s+(?:movies|films)\b", title, re.IGNORECASE))
        )
        if is_list_item and link:
            lists.append({
                "guid": guid,
                "title": title,
                "url": link,
                "date": pub_date,
            })
    return lists


def parse_letterboxd_list_html(html_text: str) -> List[Dict[str, str]]:
    """
    Extracts movie `{title, year}` items from a Letterboxd list page HTML.
    """
    movies: List[Dict[str, str]] = []
    seen = set()

    for tag_match in re.finditer(
        r"<(?:div|li|span)\b[^>]*\bdata-(?:film-name|item-name|film-slug)=[^>]*>",
        html_text,
        re.IGNORECASE,
    ):
        tag_str = tag_match.group(0)
        name_m = re.search(r'\bdata-film-name=["\']([^"\']+)["\']', tag_str, re.IGNORECASE)
        item_m = re.search(r'\bdata-item-name=["\']([^"\']+)["\']', tag_str, re.IGNORECASE)
        year_m = re.search(r'\bdata-film-release-year=["\'](\d{4})["\']', tag_str, re.IGNORECASE)
        slug_m = re.search(r'\bdata-film-slug=["\']([^"\']+)["\']', tag_str, re.IGNORECASE)

        raw_title = ""
        year = year_m.group(1) if year_m else ""
        if name_m:
            raw_title = html.unescape(name_m.group(1)).strip()
        elif item_m:
            raw_item = html.unescape(item_m.group(1)).strip()
            ym = re.search(r"^(?P<t>.+?)\s*\((?P<y>\d{4})\)\s*$", raw_item)
            if ym:
                raw_title = ym.group("t").strip()
                year = year or ym.group("y")
            else:
                raw_title = raw_item
        if not year and slug_m:
            sm = re.search(r"-(\d{4})$", slug_m.group(1))
            if sm:
                year = sm.group(1)

        title = _clean_extracted_title(raw_title)
        if not title or NON_MOVIE_TITLE_RE.search(title):
            continue
        key = (title.lower(), year or "0000")
        if key not in seen:
            seen.add(key)
            movies.append({"title": title, "year": year or "0000"})

    return movies


def reconcile_recent_lists(
    feed_urls: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Periodic reconciliation job that checks Letterboxd curated lists (`/list/` items in RSS)
    and annual/mid-year list news feeds, promoting any movie already in the library (or newly
    listed) to its curated tier (`Loved` / `Canon`) while preserving its original review text.
    """
    init_db(db_path)
    urls = feed_urls if feed_urls is not None else KOJIMA_RSS_URLS
    promoted_or_added: List[Dict[str, Any]] = []

    for url in urls:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; KojimaMovieTracker/1.0; +https://github.com/mblackman)"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                xml_text = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[warn] Failed to fetch RSS feed for list reconciliation {url}: {exc}")
            continue

        for list_entry in extract_letterboxd_lists_from_rss(xml_text):
            list_url = list_entry["url"]
            list_title = list_entry["title"]
            list_date = list_entry["date"] or time.strftime("%Y-%m-%d", time.gmtime())
            try:
                req = urllib.request.Request(
                    list_url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; KojimaMovieTracker/1.0; +https://github.com/mblackman)"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    list_html = resp.read().decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[warn] Failed to fetch curated list page {list_url}: {exc}")
                continue

            source_type, label = _infer_list_source_type(list_title, list_url)
            for film in parse_letterboxd_list_html(list_html):
                rec = upsert_recommendation(
                    {
                        "title": film["title"],
                        "year": film["year"],
                        "review": f"Named to {label}.",
                        "recommended_at": list_date,
                        "sources": [
                            {
                                "label": label,
                                "type": source_type,
                                "url": list_url,
                                "date": list_date,
                            }
                        ],
                    },
                    db_path=db_path,
                )
                if rec:
                    promoted_or_added.append(rec)

    if (
        feed_urls is None
        and KOJIMA_DISCOVER_X_REVIEWS
        and any(u.startswith(("http://", "https://")) for u in urls)
    ):
        promoted_or_added.extend(
            discover_kojima_x_reviews_from_news(
                news_rss_urls=DEFAULT_KOJIMA_LIST_NEWS_RSS_URLS,
                db_path=db_path,
            )
        )

    return promoted_or_added


def load_ledger(seed_json_path: str) -> Dict[str, Any]:
    """Reads the persisted ledger. Accepts the legacy bare-array format."""
    if not seed_json_path or not os.path.exists(seed_json_path):
        return {"last_updated": None, "seen_guids": [], "poster_checked_slugs": [], "items": []}
    try:
        with open(seed_json_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        print(f"[warn] Could not read ledger {seed_json_path}: {exc}")
        return {"last_updated": None, "seen_guids": [], "poster_checked_slugs": [], "items": []}
    if isinstance(data, list):
        return {"last_updated": None, "seen_guids": [], "poster_checked_slugs": [], "items": data}
    return {
        "last_updated": data.get("last_updated"),
        "seen_guids": list(data.get("seen_guids") or []),
        "poster_checked_slugs": list(data.get("poster_checked_slugs") or []),
        "items": data.get("items") or [],
    }


def sync_static_files(
    seed_json_path: str = "demo/seed-recommendations.json",
    api_json_path: str = "build/api/kojima-recommendations.json",
    scrape_rss: bool = True,
    reconcile_lists: bool = False,
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
         from Wikipedia (keyless) and TMDB (when TMDB_API_KEY is set), skipping slugs already
         checked in previous daily runs unless `force_refresh` or `reconcile_lists` is set.
      3. Seeds `scrape_log` with persisted `seen_guids` and all known source URLs, then polls
         KOJIMA_RSS_URLS (and when `reconcile_lists=True`, reconciles Letterboxd & annual lists).
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
        poster_checked = set() if force_refresh else set(ledger.get("poster_checked_slugs") or [])
        seen_guids_list: List[str] = list(ledger.get("seen_guids") or [])
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
        current_year = int(time.strftime("%Y", time.gmtime()))
        for raw_item in catalog:
            item = dict(raw_item)
            slug = make_slug(str(item["title"]), str(item.get("year") or "0000"))
            item_year = str(item.get("year") or "0000").strip()
            is_recent = item_year.isdigit() and int(item_year) >= current_year - 1
            should_try_enrich = enrich and not item.get("poster") and (
                slug not in poster_checked or force_refresh or (reconcile_lists and is_recent)
            )
            if should_try_enrich:
                conn = get_db_connection(db_path)
                try:
                    row = conn.execute(
                        "SELECT poster FROM recommendations WHERE slug = ?", (slug,)
                    ).fetchone()
                finally:
                    conn.close()
                if not (row and row["poster"]):
                    meta = fetch_wikipedia_metadata(str(item["title"]), str(item.get("year") or ""))
                    poster_checked.add(slug)
                    if meta.get("poster"):
                        item["poster"] = meta["poster"]
                        item["plot"] = item.get("plot") or meta.get("plot", "")
                        enriched += 1
                    time.sleep(0.1)
                else:
                    poster_checked.add(slug)
            upsert_recommendation(item, force_rescore=force_refresh, db_path=db_path)

        ingested: List[Dict[str, Any]] = []
        reconciled: List[Dict[str, Any]] = []
        if scrape_rss and KOJIMA_RSS_URLS:
            with _DB_LOCK:
                conn = get_db_connection(db_path)
                try:
                    existing_rows = conn.execute(
                        "SELECT title, tmdb_id, primary_url, sources_json FROM recommendations"
                    ).fetchall()
                    now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    for saved_guid in seen_guids_list:
                        if saved_guid:
                            conn.execute(
                                "INSERT OR IGNORE INTO scrape_log (guid, feed_url, raw_text, extracted_title, tmdb_id, scraped_at) VALUES (?, '', '', '', '', ?)",
                                (saved_guid, now_str),
                            )
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
            if reconcile_lists:
                reconciled = reconcile_recent_lists(db_path=db_path)

            with _DB_LOCK:
                conn = get_db_connection(db_path)
                try:
                    log_rows = conn.execute(
                        "SELECT guid FROM scrape_log ORDER BY scraped_at DESC LIMIT 500"
                    ).fetchall()
                    known_set = set(seen_guids_list)
                    for lr in log_rows:
                        g = str(lr["guid"] or "").strip()
                        if g and g not in known_set:
                            seen_guids_list.append(g)
                            known_set.add(g)
                    seen_guids_list = seen_guids_list[-500:]
                finally:
                    conn.close()

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
        ledger_out: Dict[str, Any] = {
            "last_updated": last_updated,
            "scoring_version": SCORING_ENGINE_VERSION,
            "count": len(ledger_items),
        }
        if seen_guids_list:
            ledger_out["seen_guids"] = seen_guids_list
        if poster_checked:
            ledger_out["poster_checked_slugs"] = sorted(poster_checked)
        ledger_out["items"] = ledger_items
        with open(seed_json_path, "w", encoding="utf-8") as fh:
            json.dump(
                ledger_out,
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
        "reconciled_count": len(reconciled),
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

    if "--sync-static" in sys.argv or "--reconcile-lists" in sys.argv:
        seed_out = os.environ.get("KOJIMA_SEED_JSON_PATH", "demo/seed-recommendations.json")
        api_out = os.environ.get("KOJIMA_API_JSON_PATH", "build/api/kojima-recommendations.json")
        payload = sync_static_files(
            seed_json_path=seed_out,
            api_json_path=api_out,
            scrape_rss=True,
            reconcile_lists="--reconcile-lists" in sys.argv,
            force_refresh="--force-rescore" in sys.argv,
            enrich="--no-enrich" not in sys.argv and os.environ.get("KOJIMA_ENRICH", "1") != "0",
        )
        print(
            f"✅ {payload['count']} Kojima recommendations "
            f"(+{payload['newly_ingested_count']} from RSS, {payload['reconciled_count']} list items reconciled, "
            f"{payload['enriched_count']} posters enriched, "
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
