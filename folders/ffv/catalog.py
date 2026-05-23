"""
FFV static catalogs: subreddit tone maps, render-phase documentation, SFX heuristics.

Loaded by the engine for diversity hints, help output, and weighted SFX family balancing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, FrozenSet, Iterable

# Theory timings (seconds) — single source for catalog timeline text
REACTABLE_HOLD = 5.0
REACTION_HOLD = 3.5
SEGMENT_VISUAL_TOTAL = REACTABLE_HOLD + REACTION_HOLD

# Merged into the reactable pool after parsing theory.txt (deduped).
# Verbal roasts, comebacks, screenshot cringe, and social-post burns (rareinsults-adjacent).
_FFV_EXTRA_REACTABLE_SUB_NAMES: tuple[str, ...] = (
    "rareinsults",
    "murderedbywords",
    "clevercomebacks",
    "suicidebywords",
    "quityourbullshit",
    "confidentlyincorrect",
    "agedlikemilk",
    "agedlikewine",
    "oopsdidntmeanto",
    "nothingeverhappens",
    "thathappened",
    "iamatotalpieceofshit",
    "entitledpeople",
    "choosingbeggars",
    "insanepeoplefacebook",
    "insanepeopletwitter",
    "iamverybadass",
    "iamverysmart",
    "justneckbeardthings",
    "niceguys",
    "nicegirls",
    "creepyasterisks",
    "creepypms",
    "beholdthemasterrace",
    "forwardsfromgrandma",
    "terriblefacebookmemes",
    "comedycemetery",
    "comedyhomicide",
    "woooosh",
    "leopardsatemyface",
    "selfawarewolves",
    "dontyouknowwhoiam",
    "idontworkherelady",
    "maliciouscompliance",
    "prorevenge",
    "nuclearrevenge",
    "pettyrevenge",
    "entitledparents",
    "insaneparents",
    "tiktokcringe",
    "im14andthisisdeep",
    "notliketheothergirls",
    "badwomensanatomy",
    "nothowgirlswork",
    "menwritingwomen",
    "pointlesslygendered",
    "insanepeoplequora",
    "linkedinlunatics",
    "tinder",
    "whitepeopletwitter",
    "blackpeopletwitter",
)
FFV_EXTRA_REACTABLE_SUBS: frozenset[str] = frozenset(_FFV_EXTRA_REACTABLE_SUB_NAMES)

# Subs fetched first for Reddit image pools (still-heavy); rest are shuffled after.
FFV_POOL_FETCH_PRIORITY_REACTABLES: frozenset[str] = frozenset(
    {
        "hmmm",
        "pics",
        "memes",
        "funny",
        "dankmemes",
        "abruptchaos",
        "unexpected",
        "crappydesign",
        "facepalm",
        "oddlysatisfying",
        "interestingasfuck",
        "mildlyinteresting",
        "blursedimages",
        "comedyheaven",
        "atbge",
        "perfecttiming",
        "holup",
        "whatcouldgowrong",
        "instant_regret",
        "wellthatsucks",
        "youseeingthisshit",
        "nextfuckinglevel",
        "technicallythetruth",
        "damnthatsinteresting",
        "cursedcomments",
        "nonononoyes",
        "yesyesyesyesno",
        "therewasanattempt",
        "confusing_perspective",
        "aww",
        "earthporn",
        "cityporn",
    }
)
FFV_POOL_FETCH_PRIORITY_REACTIONS: frozenset[str] = frozenset(
    {
        "reactionimages",
        "deepfriedmemes",
        "deepfreezedmemes",
        "memetemplatesofficial",
        "reactionpics",
        "okbuddyretard",
    }
)

# ---------------------------------------------------------------------------
# Theory-derived reactable subreddits (display + diversity)
# ---------------------------------------------------------------------------

REACTABLE_CLUSTER: dict[str, dict[str, Any]] = {
    "accidentaltopgear": {
        "display": "r/accidentaltopgear",
        "archetype": "british_panel_chaos",
        "energy": 0.78,
        "tags": frozenset({"vehicles", "irony", "caption_humor"}),
        "diversity_weight": 1.05,
        "notes": "Top Gear / meme adjacent accidental framing.",
    },
    "CrappyDesign": {
        "display": "r/CrappyDesign",
        "archetype": "design_fail",
        "energy": 0.62,
        "tags": frozenset({"objects", "ux", "wtf_layout"}),
        "diversity_weight": 1.12,
        "notes": "Objects and interfaces that violate common sense.",
    },
    "Whatcouldgowrong": {
        "display": "r/Whatcouldgowrong",
        "archetype": "predictable_disaster",
        "energy": 0.88,
        "tags": frozenset({"stunts", "physics", "consequences"}),
        "diversity_weight": 1.08,
        "notes": "Setup obvious; punchline is kinetic failure.",
    },
    "trollscience": {
        "display": "r/trollscience",
        "archetype": "pseudo_educational_meme",
        "energy": 0.55,
        "tags": frozenset({"diagrams", "absurd_logic", "school_vibes"}),
        "diversity_weight": 0.98,
        "notes": "Fake science diagrams with confident wrongness.",
    },
    "hmmm": {
        "display": "r/hmmm",
        "archetype": "liminal_uncanny",
        "energy": 0.42,
        "tags": frozenset({"ambient", "low_context", "weird_still"}),
        "diversity_weight": 1.15,
        "notes": "Single image that refuses to resolve semantically.",
    },
    "AbruptChaos": {
        "display": "r/AbruptChaos",
        "archetype": "whiplash_cut",
        "energy": 0.92,
        "tags": frozenset({"crowd", "sudden_motion", "cursed_timing"}),
        "diversity_weight": 1.1,
        "notes": "Calm → explosion; great for reaction pacing.",
    },
    "assholedesign": {
        "display": "r/assholedesign",
        "archetype": "malicious_compliance_ui",
        "energy": 0.7,
        "tags": frozenset({"dark_patterns", "capitalism_meme", "software"}),
        "diversity_weight": 1.04,
        "notes": "Hostile UX presented as straight documentation.",
    },
    "ATBGE": {
        "display": "r/ATBGE",
        "archetype": "great_execution_bad_taste",
        "energy": 0.66,
        "tags": frozenset({"craft", "aesthetic_clash", "objects"}),
        "diversity_weight": 1.0,
        "notes": "Awful taste but undeniable skill.",
    },
    "brandnewsentence": {
        "display": "r/brandnewsentence",
        "archetype": "linguistic_glitch",
        "energy": 0.5,
        "tags": frozenset({"text", "headline", "cursed_grammar"}),
        "diversity_weight": 0.95,
        "notes": "Sentences that should not exist yet do.",
    },
    "Confusing_Perspective": {
        "display": "r/Confusing_Perspective",
        "archetype": "visual_paradox",
        "energy": 0.58,
        "tags": frozenset({"forced_perspective", "optical", "still_image"}),
        "diversity_weight": 1.02,
        "notes": "Brain refuses depth parsing; holds attention.",
    },
    "engrish": {
        "display": "r/engrish",
        "archetype": "translation_artifact",
        "energy": 0.48,
        "tags": frozenset({"signs", "packaging", "text"}),
        "diversity_weight": 0.93,
        "notes": "Unintentional poetry from mistranslation.",
    },
    "facepalm": {
        "display": "r/facepalm",
        "archetype": "human_decision_fail",
        "energy": 0.64,
        "tags": frozenset({"politics_adjacent", "screenshots", "comments"}),
        "diversity_weight": 1.18,
        "notes": "Broad catch-all for human folly; high throughput.",
    },
    "HolUp": {
        "display": "r/HolUp",
        "archetype": "delayed_realization",
        "energy": 0.74,
        "tags": frozenset({"twist", "second_read", "image_text"}),
        "diversity_weight": 1.07,
        "notes": "First glance normal; detail breaks it.",
    },
    "IdiotsInCars": {
        "display": "r/IdiotsInCars",
        "archetype": "traffic_absurdism",
        "energy": 0.85,
        "tags": frozenset({"dashcam", "roads", "kinetics"}),
        "diversity_weight": 1.09,
        "notes": "Vehicles as protagonists of bad choices.",
    },
    "instant_regret": {
        "display": "r/instant_regret",
        "archetype": "immediate_consequence",
        "energy": 0.9,
        "tags": frozenset({"stunt_fail", "self_inflicted", "fast_payoff"}),
        "diversity_weight": 1.06,
        "notes": "Action and regret separated by milliseconds.",
    },
    "Justfuckmyshitup": {
        "display": "r/Justfuckmyshitup",
        "archetype": "hair_and_image_disaster",
        "energy": 0.72,
        "tags": frozenset({"haircut", "portrait", "confidence_gap"}),
        "diversity_weight": 0.99,
        "notes": "Visual mismatch between intent and outcome.",
    },
    "notdisneyvacation": {
        "display": "r/notdisneyvacation",
        "archetype": "cursed_stock_photo",
        "energy": 0.52,
        "tags": frozenset({"travel", "uncanny_family", "marketing_fail"}),
        "diversity_weight": 0.97,
        "notes": "Tourism marketing that becomes horror adjacent.",
    },
    "PerfectTiming": {
        "display": "r/PerfectTiming",
        "archetype": "shutter_luck",
        "energy": 0.68,
        "tags": frozenset({"photography", "single_frame", "coincidence"}),
        "diversity_weight": 1.03,
        "notes": "Peak moment frozen; pairs with tight sfx hits.",
    },
    "ShittyLifeProTips": {
        "display": "r/ShittyLifeProTips",
        "archetype": "ironic_advice",
        "energy": 0.46,
        "tags": frozenset({"text_meme", "tutorial_spoof"}),
        "diversity_weight": 0.94,
        "notes": "SLPT format; deadpan harmful guidance.",
    },
    "therewasanattempt": {
        "display": "r/therewasanattempt",
        "archetype": "effort_without_payoff",
        "energy": 0.6,
        "tags": frozenset({"wholesome_fail", "sports", "craft"}),
        "diversity_weight": 1.01,
        "notes": "Sincerity collides with reality.",
    },
    "Unexpected": {
        "display": "r/Unexpected",
        "archetype": "genre_bend",
        "energy": 0.8,
        "tags": frozenset({"twist", "video_adjacent_still", "punchline"}),
        "diversity_weight": 1.14,
        "notes": "Setup bait; payoff from another dimension.",
    },
    "WatchPeopleDieInside": {
        "display": "r/WatchPeopleDieInside",
        "archetype": "emotional_collapse_micro",
        "energy": 0.56,
        "tags": frozenset({"faces", "secondhand_cringe", "quiet_chaos"}),
        "diversity_weight": 1.11,
        "notes": "Facial micro-performances of regret.",
    },
    "Wellthatsucks": {
        "display": "r/Wellthatsucks",
        "archetype": "mundane_catastrophe",
        "energy": 0.59,
        "tags": frozenset({"everyday_fail", "property_damage", "irony"}),
        "diversity_weight": 1.0,
        "notes": "Relatable small disasters with big feelings.",
    },
    "WinStupidPrizes": {
        "display": "r/WinStupidPrizes",
        "archetype": "stupid_game_stupid_prize",
        "energy": 0.87,
        "tags": frozenset({"consequences", "darwin_adjacent", "kinetics"}),
        "diversity_weight": 1.05,
        "notes": "Actions chosen; prizes earned.",
    },
    "youseeingthisshit": {
        "display": "r/youseeingthisshit",
        "archetype": "witness_reaction_in_frame",
        "energy": 0.63,
        "tags": frozenset({"animals", "side_eye", "duo_composition"}),
        "diversity_weight": 1.02,
        "notes": "Someone in-frame refuses the premise.",
    },
    "oldpeoplefacebook": {
        "display": "r/oldpeoplefacebook",
        "archetype": "social_media_boomer_chaos",
        "energy": 0.58,
        "tags": frozenset({"comments", "facebook", "accidental_comedy"}),
        "diversity_weight": 1.2,
        "notes": "Peak comment-thread confusion and wholesome chaos.",
    },
    "insanepeoplefacebook": {
        "display": "r/insanepeoplefacebook",
        "archetype": "social_media_unhinged_post",
        "energy": 0.76,
        "tags": frozenset({"screenshots", "comments", "arguments"}),
        "diversity_weight": 1.15,
        "notes": "Unhinged timelines and escalating thread energy.",
    },
    "WhitePeopleTwitter": {
        "display": "r/WhitePeopleTwitter",
        "archetype": "twitter_quote_chain",
        "energy": 0.64,
        "tags": frozenset({"tweets", "ratio", "reply_threads"}),
        "diversity_weight": 1.12,
        "notes": "Screenshotted tweets with strong reply context.",
    },
    "clevercomebacks": {
        "display": "r/clevercomebacks",
        "archetype": "comment_section_finisher",
        "energy": 0.62,
        "tags": frozenset({"comments", "roast", "one_liner"}),
        "diversity_weight": 1.08,
        "notes": "Reply-chain killshots and concise punchlines.",
    },
    "rareinsults": {
        "display": "r/rareinsults",
        "archetype": "verbal_creative_destruction",
        "energy": 0.67,
        "tags": frozenset({"comments", "screenshots", "insults"}),
        "diversity_weight": 1.06,
        "notes": "Creative one-off insults perfect for social post reactions.",
    },
    "twitter": {
        "display": "r/twitter",
        "archetype": "platform_screenshot_stream",
        "energy": 0.64,
        "tags": frozenset({"tweets", "screenshots", "social_media"}),
        "diversity_weight": 1.2,
        "notes": "General Twitter screenshot feed.",
    },
    "BlackPeopleTwitter": {
        "display": "r/BlackPeopleTwitter",
        "archetype": "tweet_screenshot_quote_chain",
        "energy": 0.69,
        "tags": frozenset({"tweets", "screenshots", "reply_threads"}),
        "diversity_weight": 1.22,
        "notes": "High-volume social screenshot source.",
    },
    "nonpoliticaltwitter": {
        "display": "r/nonpoliticaltwitter",
        "archetype": "tweet_screenshot_nonpolitical",
        "energy": 0.57,
        "tags": frozenset({"tweets", "screenshots", "social_media"}),
        "diversity_weight": 1.18,
        "notes": "General tweet screenshots with less political skew.",
    },
    "insanepeoplequora": {
        "display": "r/insanepeoplequora",
        "archetype": "qa_screenshot_absurdism",
        "energy": 0.66,
        "tags": frozenset({"screenshots", "qna", "social_media"}),
        "diversity_weight": 1.16,
        "notes": "Screenshot-heavy Q/A absurdity feed.",
    },
    "LinkedInLunatics": {
        "display": "r/LinkedInLunatics",
        "archetype": "professional_network_post_cringe",
        "energy": 0.63,
        "tags": frozenset({"screenshots", "linkedin", "posts"}),
        "diversity_weight": 1.18,
        "notes": "LinkedIn post screenshots and comment chains.",
    },
    "im14andthisisdeep": {
        "display": "r/im14andthisisdeep",
        "archetype": "quote_post_mockery",
        "energy": 0.52,
        "tags": frozenset({"screenshots", "quotes", "social_posts"}),
        "diversity_weight": 1.1,
        "notes": "Over-serious quote/image social posts.",
    },
    "terriblefacebookmemes": {
        "display": "r/terriblefacebookmemes",
        "archetype": "facebook_screenshot_boomer_meme",
        "energy": 0.59,
        "tags": frozenset({"facebook", "screenshots", "memes"}),
        "diversity_weight": 1.17,
        "notes": "Screenshot source with explicitly social feed format.",
    },
    "choosingbeggars": {
        "display": "r/choosingbeggars",
        "archetype": "message_screenshot_entitlement",
        "energy": 0.61,
        "tags": frozenset({"screenshots", "messages", "threads"}),
        "diversity_weight": 1.14,
        "notes": "DM and listing screenshots with social post feel.",
    },
    "antiwork": {
        "display": "r/antiwork",
        "archetype": "workplace_text_screenshot",
        "energy": 0.6,
        "tags": frozenset({"texts", "screenshots", "posts"}),
        "diversity_weight": 1.08,
        "notes": "Frequent text/thread screenshots and post captures.",
    },
    "AmItheAsshole": {
        "display": "r/AmItheAsshole",
        "archetype": "story_post_screenshot",
        "energy": 0.56,
        "tags": frozenset({"post_text", "screenshots", "social_drama"}),
        "diversity_weight": 1.07,
        "notes": "Long-form social-style post screenshots and story cards.",
    },
    "amithedevil": {
        "display": "r/amithedevil",
        "archetype": "crosspost_story_screenshot",
        "energy": 0.58,
        "tags": frozenset({"post_text", "screenshots", "social_drama"}),
        "diversity_weight": 1.06,
        "notes": "AITA-adjacent screenshot-rich story posts.",
    },
    "texts": {
        "display": "r/texts",
        "archetype": "conversation_screenshot",
        "energy": 0.62,
        "tags": frozenset({"messages", "screenshots", "social"}),
        "diversity_weight": 1.13,
        "notes": "Direct message screenshot pool.",
    },
    "Tinder": {
        "display": "r/Tinder",
        "archetype": "dating_app_chat_screenshot",
        "energy": 0.68,
        "tags": frozenset({"messages", "screenshots", "dating_app"}),
        "diversity_weight": 1.12,
        "notes": "Dating app screenshot conversation posts.",
    },
    "discordVideos": {
        "display": "r/discordVideos",
        "archetype": "discord_capture_social_clip",
        "energy": 0.64,
        "tags": frozenset({"discord", "screenshots", "social_media"}),
        "diversity_weight": 1.05,
        "notes": "Discord-centric social captures and reposts.",
    },
    "sadcringe": {
        "display": "r/sadcringe",
        "archetype": "social_post_cringe_capture",
        "energy": 0.65,
        "tags": frozenset({"screenshots", "posts", "cringe"}),
        "diversity_weight": 1.09,
        "notes": "Plenty of social screenshot cringe content.",
    },
    "madlads": {
        "display": "r/madlads",
        "archetype": "comment_thread_lad_energy",
        "energy": 0.67,
        "tags": frozenset({"screenshots", "comments", "social_posts"}),
        "diversity_weight": 1.08,
        "notes": "Social feed screenshot posts with chaotic comments.",
    },
    "OutOfTheLoop": {
        "display": "r/OutOfTheLoop",
        "archetype": "thread_screenshot_context_posts",
        "energy": 0.53,
        "tags": frozenset({"threads", "screenshots", "social_posts"}),
        "diversity_weight": 1.06,
        "notes": "Context-heavy social thread screenshots and summaries.",
    },
    "bestof": {
        "display": "r/bestof",
        "archetype": "comment_chain_highlights",
        "energy": 0.5,
        "tags": frozenset({"comments", "threads", "social_posts"}),
        "diversity_weight": 1.03,
        "notes": "Highlight posts often sourced from social thread captures.",
    },
    "SubredditDrama": {
        "display": "r/SubredditDrama",
        "archetype": "social_drama_screenshot",
        "energy": 0.68,
        "tags": frozenset({"threads", "screenshots", "arguments"}),
        "diversity_weight": 1.1,
        "notes": "Drama threads and screenshots with social platform feel.",
    },
    "LeopardsAteMyFace": {
        "display": "r/LeopardsAteMyFace",
        "archetype": "social_post_consequence_screenshot",
        "energy": 0.65,
        "tags": frozenset({"screenshots", "posts", "threads"}),
        "diversity_weight": 1.07,
        "notes": "Frequent social screenshot posts and quote captures.",
    },
    "selfawarewolves": {
        "display": "r/selfawarewolves",
        "archetype": "screenshot_irony_threads",
        "energy": 0.61,
        "tags": frozenset({"screenshots", "tweets", "comments"}),
        "diversity_weight": 1.08,
        "notes": "Social screenshots with irony-heavy commentary.",
    },
    "confidentlyincorrect": {
        "display": "r/confidentlyincorrect",
        "archetype": "wrong_take_screenshot_feed",
        "energy": 0.6,
        "tags": frozenset({"screenshots", "comments", "posts"}),
        "diversity_weight": 1.09,
        "notes": "Screenshot-heavy incorrect take compilations.",
    },
    "GatekeepingYuri": {
        "display": "r/GatekeepingYuri",
        "archetype": "post_screenshot_rewrite",
        "energy": 0.47,
        "tags": frozenset({"screenshots", "social_posts", "captions"}),
        "diversity_weight": 1.0,
        "notes": "Often based on social post screenshot edits.",
    },
    "MurderedByWords": {
        "display": "r/MurderedByWords",
        "archetype": "reply_chain_ownage",
        "energy": 0.7,
        "tags": frozenset({"comments", "screenshots", "tweets"}),
        "diversity_weight": 1.12,
        "notes": "Strong social screenshot and reply-chain content.",
    },
    "iamatotalpieceofshit": {
        "display": "r/iamatotalpieceofshit",
        "archetype": "social_callout_screenshots",
        "energy": 0.72,
        "tags": frozenset({"screenshots", "posts", "social_media"}),
        "diversity_weight": 1.06,
        "notes": "Callout posts frequently shared as screenshot captures.",
    },
    "trashy": {
        "display": "r/trashy",
        "archetype": "social_capture_shock_feed",
        "energy": 0.71,
        "tags": frozenset({"screenshots", "posts", "reactions"}),
        "diversity_weight": 1.04,
        "notes": "General shock-value social captures.",
    },
    "NoahGetTheBoat": {
        "display": "r/NoahGetTheBoat",
        "archetype": "horrified_social_screenshot",
        "energy": 0.73,
        "tags": frozenset({"screenshots", "posts", "comments"}),
        "diversity_weight": 1.05,
        "notes": "Heavily screenshot-driven social media content.",
    },
    "mildlyinfuriating": {
        "display": "r/mildlyinfuriating",
        "archetype": "complaint_post_screenshots",
        "energy": 0.58,
        "tags": frozenset({"screenshots", "texts", "social_posts"}),
        "diversity_weight": 1.07,
        "notes": "Frequent screenshot complaints and social-post captures.",
    },
    "askreddit": {
        "display": "r/AskReddit",
        "archetype": "comment_thread_screenshot_source",
        "energy": 0.55,
        "tags": frozenset({"threads", "comments", "social_posts"}),
        "diversity_weight": 1.03,
        "notes": "Huge thread source often reposted as screenshot compilations.",
    },
    "nostupidquestions": {
        "display": "r/NoStupidQuestions",
        "archetype": "qa_thread_capture",
        "energy": 0.48,
        "tags": frozenset({"threads", "comments", "screenshots"}),
        "diversity_weight": 1.01,
        "notes": "Q/A thread screenshots and social post captures.",
    },
    "TooAfraidToAsk": {
        "display": "r/TooAfraidToAsk",
        "archetype": "taboo_qa_social_thread",
        "energy": 0.52,
        "tags": frozenset({"threads", "screenshots", "social_posts"}),
        "diversity_weight": 1.02,
        "notes": "Question-thread captures with social screenshot vibe.",
    },
    "relationship_advice": {
        "display": "r/relationship_advice",
        "archetype": "story_post_social_drama",
        "energy": 0.59,
        "tags": frozenset({"post_text", "threads", "screenshots"}),
        "diversity_weight": 1.04,
        "notes": "Story-style post screenshots and relationship drama threads.",
    },
    "offmychest": {
        "display": "r/offmychest",
        "archetype": "confession_post_screenshot",
        "energy": 0.51,
        "tags": frozenset({"post_text", "social_posts", "screenshots"}),
        "diversity_weight": 1.0,
        "notes": "Confession-style social posts often repackaged as screenshots.",
    },
    "TrueOffMyChest": {
        "display": "r/TrueOffMyChest",
        "archetype": "confession_thread_social_capture",
        "energy": 0.53,
        "tags": frozenset({"post_text", "threads", "screenshots"}),
        "diversity_weight": 1.0,
        "notes": "Thread-heavy confession posts suitable for social screenshot style.",
    },
    "NotHowGirlsWork": {
        "display": "r/NotHowGirlsWork",
        "archetype": "social_post_take_screenshot",
        "energy": 0.62,
        "tags": frozenset({"screenshots", "tweets", "posts"}),
        "diversity_weight": 1.06,
        "notes": "Screenshot-based bad takes from social platforms.",
    },
    "badwomensanatomy": {
        "display": "r/badwomensanatomy",
        "archetype": "bad_take_social_screenshots",
        "energy": 0.6,
        "tags": frozenset({"screenshots", "posts", "comments"}),
        "diversity_weight": 1.04,
        "notes": "Social screenshot examples of incorrect claims.",
    },
    "niceguys": {
        "display": "r/niceguys",
        "archetype": "dm_screenshot_cringe",
        "energy": 0.67,
        "tags": frozenset({"messages", "screenshots", "social_posts"}),
        "diversity_weight": 1.09,
        "notes": "Message screenshot archive with social feed format.",
    },
    "nicegirls": {
        "display": "r/nicegirls",
        "archetype": "chat_screenshot_cringe",
        "energy": 0.66,
        "tags": frozenset({"messages", "screenshots", "social_posts"}),
        "diversity_weight": 1.08,
        "notes": "Conversation screenshot counterpart feed.",
    },
    "insaneparents": {
        "display": "r/insaneparents",
        "archetype": "family_text_screenshot",
        "energy": 0.64,
        "tags": frozenset({"texts", "screenshots", "threads"}),
        "diversity_weight": 1.07,
        "notes": "Text-thread screenshots with strong social-story vibe.",
    },
    "entitledparents": {
        "display": "r/entitledparents",
        "archetype": "story_screenshot_entitlement",
        "energy": 0.61,
        "tags": frozenset({"post_text", "screenshots", "social_drama"}),
        "diversity_weight": 1.05,
        "notes": "Story and screenshot format entitlement posts.",
    },
    "funny": {
        "display": "r/funny",
        "archetype": "mainstream_meme_feed",
        "energy": 0.66,
        "tags": frozenset({"general", "image_macro", "viral"}),
        "diversity_weight": 1.0,
        "notes": "High-volume general humor pool.",
    },
    "memes": {
        "display": "r/memes",
        "archetype": "image_macro_general",
        "energy": 0.7,
        "tags": frozenset({"general", "caption", "template"}),
        "diversity_weight": 1.03,
        "notes": "Broad meme templates and caption formats.",
    },
    "dankmemes": {
        "display": "r/dankmemes",
        "archetype": "edgy_meme_stream",
        "energy": 0.78,
        "tags": frozenset({"meme", "irony", "high_variance"}),
        "diversity_weight": 1.02,
        "notes": "Fast-moving meme pool with high turnover.",
    },
    "wholesomememes": {
        "display": "r/wholesomememes",
        "archetype": "positive_meme_break",
        "energy": 0.42,
        "tags": frozenset({"meme", "wholesome", "text_image"}),
        "diversity_weight": 0.95,
        "notes": "Lower-intensity meme cadence for pacing contrast.",
    },
    "me_irl": {
        "display": "r/me_irl",
        "archetype": "relatable_reaction_meme",
        "energy": 0.61,
        "tags": frozenset({"reaction", "caption", "relatable"}),
        "diversity_weight": 1.01,
        "notes": "Relatable image jokes and reaction stills.",
    },
    "meirl": {
        "display": "r/meirl",
        "archetype": "relatable_meme_variant",
        "energy": 0.6,
        "tags": frozenset({"reaction", "caption", "general"}),
        "diversity_weight": 1.0,
        "notes": "Alternate me_irl spelling with distinct feed mix.",
    },
    "pics": {
        "display": "r/pics",
        "archetype": "general_photo_stream",
        "energy": 0.52,
        "tags": frozenset({"photos", "general", "real_world"}),
        "diversity_weight": 1.04,
        "notes": "Large general image feed beyond pure memes.",
    },
    "interestingasfuck": {
        "display": "r/interestingasfuck",
        "archetype": "visual_curiosity_dump",
        "energy": 0.57,
        "tags": frozenset({"photos", "novelty", "oddities"}),
        "diversity_weight": 1.07,
        "notes": "Broadly interesting visual posts.",
    },
    "Damnthatsinteresting": {
        "display": "r/Damnthatsinteresting",
        "archetype": "curiosity_showcase",
        "energy": 0.56,
        "tags": frozenset({"photos", "facts", "novelty"}),
        "diversity_weight": 1.06,
        "notes": "General interest visuals with broad subject range.",
    },
    "mildlyinteresting": {
        "display": "r/mildlyinteresting",
        "archetype": "subtle_visual_oddity",
        "energy": 0.39,
        "tags": frozenset({"photos", "objects", "low_intensity"}),
        "diversity_weight": 1.08,
        "notes": "Calmer oddities for pacing variation.",
    },
    "nextfuckinglevel": {
        "display": "r/nextfuckinglevel",
        "archetype": "wow_factor_showcase",
        "energy": 0.75,
        "tags": frozenset({"skill", "spectacle", "photo_video_stills"}),
        "diversity_weight": 1.02,
        "notes": "High wow-factor clips and stills.",
    },
    "oddlysatisfying": {
        "display": "r/oddlysatisfying",
        "archetype": "pattern_pleasing_visuals",
        "energy": 0.43,
        "tags": frozenset({"textures", "patterns", "calming"}),
        "diversity_weight": 1.03,
        "notes": "Pleasant visual content to diversify tone.",
    },
    "technicallythetruth": {
        "display": "r/technicallythetruth",
        "archetype": "literalism_meme",
        "energy": 0.5,
        "tags": frozenset({"text", "screenshots", "wordplay"}),
        "diversity_weight": 0.98,
        "notes": "Literal punchline screenshots.",
    },
    "cursedcomments": {
        "display": "r/cursedcomments",
        "archetype": "comment_thread_chaos",
        "energy": 0.64,
        "tags": frozenset({"comments", "screenshots", "wtf"}),
        "diversity_weight": 1.09,
        "notes": "General cursed screenshot pool.",
    },
    "facepalmclassic": {
        "display": "r/facepalmclassic",
        "archetype": "legacy_facepalm_archive",
        "energy": 0.58,
        "tags": frozenset({"screenshots", "fails", "reaction"}),
        "diversity_weight": 0.96,
        "notes": "Extra facepalm-like pool for broader source coverage.",
    },
    "comedyheaven": {
        "display": "r/comedyheaven",
        "archetype": "absurd_low_fidelity_meme",
        "energy": 0.63,
        "tags": frozenset({"absurd", "meme", "image_macro"}),
        "diversity_weight": 1.01,
        "notes": "Unintentionally funny / absurd meme format stream.",
    },
    "blursedimages": {
        "display": "r/blursedimages",
        "archetype": "blessed_cursed_hybrid",
        "energy": 0.59,
        "tags": frozenset({"weird", "photos", "uncanny"}),
        "diversity_weight": 1.05,
        "notes": "General uncanny image pool.",
    },
    "nonononoyes": {
        "display": "r/nonononoyes",
        "archetype": "disaster_recovery_arc",
        "energy": 0.79,
        "tags": frozenset({"tension", "near_miss", "payoff"}),
        "diversity_weight": 1.0,
        "notes": "Near-fail to success arc clips/stills.",
    },
    "yesyesyesyesno": {
        "display": "r/yesyesyesyesno",
        "archetype": "success_to_fail_arc",
        "energy": 0.8,
        "tags": frozenset({"tension", "reversal", "payoff"}),
        "diversity_weight": 1.0,
        "notes": "Inverse arc for reaction variety.",
    },
}

REACTION_CLUSTER: dict[str, dict[str, Any]] = {
    "DeepFreezedMemes": {
        "display": "r/DeepFreezedMemes",
        "archetype": "hypercompressed_irony",
        "energy": 0.77,
        "tags": frozenset({"deep_fried_adjacent", "caption_meme"}),
        "diversity_weight": 1.06,
        "notes": "Cold-storage meme aesthetics; crunchy pixels.",
    },
    "DeepFriedMemes": {
        "display": "r/DeepFriedMemes",
        "archetype": "saturated_chaos_still",
        "energy": 0.84,
        "tags": frozenset({"lens_flare", "ironic_font", "visual_noise"}),
        "diversity_weight": 1.12,
        "notes": "High visual entropy reaction plates.",
    },
    "reactionimages": {
        "display": "r/reactionimages",
        "archetype": "stock_reaction_template",
        "energy": 0.61,
        "tags": frozenset({"template", "caption_ready", "classic"}),
        "diversity_weight": 1.2,
        "notes": "General-purpose reaction stills; broad coverage.",
    },
    "MemeTemplatesOfficial": {
        "display": "r/MemeTemplatesOfficial",
        "archetype": "template_reaction_source",
        "energy": 0.54,
        "tags": frozenset({"templates", "reaction", "captions"}),
        "diversity_weight": 1.08,
        "notes": "Large template bank for reaction plate selection.",
    },
    "reactionpics": {
        "display": "r/reactionpics",
        "archetype": "photo_reaction_frames",
        "energy": 0.57,
        "tags": frozenset({"reaction", "photo", "faces"}),
        "diversity_weight": 1.05,
        "notes": "Additional reaction-image feed with facial focus.",
    },
    "okbuddyretard": {
        "display": "r/okbuddyretard",
        "archetype": "chaotic_reaction_meme",
        "energy": 0.74,
        "tags": frozenset({"meme", "reaction", "absurd"}),
        "diversity_weight": 0.98,
        "notes": "Chaotic reaction memes for stronger punchline contrast.",
    },
}

# ---------------------------------------------------------------------------
# LOL preset: “caption this chaos” — weird/funny stills → meme reactions → meme SFX
# ---------------------------------------------------------------------------

_FFV_FUNNY_PRESET_REACTABLES_CORE: frozenset[str] = frozenset(
    {
        "hmmm",
        "HolUp",
        "AbruptChaos",
        "Unexpected",
        "accidentaltopgear",
        "CrappyDesign",
        "facepalm",
        "instant_regret",
        "Whatcouldgowrong",
        "Wellthatsucks",
        "youseeingthisshit",
        "PerfectTiming",
        "therewasanattempt",
        "ATBGE",
        "trollscience",
        "brandnewsentence",
        "engrish",
        "notdisneyvacation",
        "ShittyLifeProTips",
        "IdiotsInCars",
        "WinStupidPrizes",
        "Justfuckmyshitup",
        "Confusing_Perspective",
        "terriblefacebookmemes",
        "funny",
        "memes",
        "dankmemes",
        "me_irl",
        "meirl",
        "comedyheaven",
        "nonononoyes",
        "yesyesyesyesno",
        "WatchPeopleDieInside",
        "oldpeoplefacebook",
        "madlads",
        "assholedesign",
    }
)
FFV_FUNNY_PRESET_REACTABLES: frozenset[str] = (
    _FFV_FUNNY_PRESET_REACTABLES_CORE | FFV_EXTRA_REACTABLE_SUBS
)

FFV_FUNNY_PRESET_REACTIONS: frozenset[str] = frozenset(
    {
        "reactionimages",
        "DeepFriedMemes",
        "DeepFreezedMemes",
        "MemeTemplatesOfficial",
        "reactionpics",
        "okbuddyretard",
    }
)

# Served first in LOL mode so picks skew toward classic meme hits (vine boom, bruh, oof, …).
FFV_FUNNY_SFX_PRIORITY_URLS: tuple[str, ...] = (
    "https://www.myinstants.com/en/instant/vine-boom-sound-70972/",
    "https://www.myinstants.com/en/instant/bruh/",
    "https://www.myinstants.com/en/instant/metal-pipe-clang-80894/",
    "https://www.myinstants.com/en/instant/roblox-oof-43192/",
    "https://www.myinstants.com/en/instant/mlg-air-horn-3718/",
    "https://www.myinstants.com/en/instant/anime-wow-sound-89672/",
    "https://www.myinstants.com/en/instant/emotional-damage-meme-82729/",
    "https://www.myinstants.com/en/instant/discord-notification-38119/",
    "https://www.myinstants.com/en/instant/windows-xp-error-42042/",
    "https://www.myinstants.com/en/instant/galaxy-meme-18643/",
    "https://www.myinstants.com/en/instant/to-be-continued-jojo-7117/",
    "https://www.myinstants.com/en/instant/among-us-role-reveal-sound-34956/",
    "https://www.myinstants.com/en/instant/a-few-moments-later-sponge-bob-sfx-fun-80331/",
    "https://www.myinstants.com/en/instant/flashbang-gah-dayum-64535/",
)

# ---------------------------------------------------------------------------
# Render / orchestration phase registry (documentation + `help deep`)
# ---------------------------------------------------------------------------

FFV_RENDER_PHASES: list[dict[str, Any]] = [
    {
        "phase_id": "FFV_P00_SESSION_ALLOC",
        "lane": "session",
        "summary": "Allocate UUID session id and ensure asset/session dirs exist.",
        "audit": "session_bootstrap",
        "timeout_profile": "instant",
    },
    {
        "phase_id": "FFV_P01_CORPUS_LOAD",
        "lane": "theory",
        "summary": "Parse theory.txt sections: Reactables, Reactions, Sfx URL list.",
        "audit": "corpus_digest",
        "timeout_profile": "instant",
    },
    {
        "phase_id": "FFV_P02_SEED_HYDRATE",
        "lane": "rng",
        "summary": "Apply optional user seed to Python random module.",
        "audit": None,
        "timeout_profile": "instant",
    },
    {
        "phase_id": "FFV_P03_SUB_SAMPLE_PLAN",
        "lane": "reddit_plan",
        "summary": "Choose subset of reactable subs to scrape (bandwidth limiter).",
        "audit": "sub_pick",
        "timeout_profile": "instant",
    },
    {
        "phase_id": "FFV_P04_REDDIT_TLS",
        "lane": "network",
        "summary": "Establish TLS session to reddit.com with configured User-Agent.",
        "audit": None,
        "timeout_profile": "short_io",
    },
    {
        "phase_id": "FFV_P05_HOT_JSON_FETCH",
        "lane": "reddit_io",
        "summary": "GET /r/{sub}/hot.json with limit and raw_json=1.",
        "audit": "reddit_http_meta",
        "timeout_profile": "reddit",
    },
    {
        "phase_id": "FFV_P06_CHILD_FILTER_IMAGE",
        "lane": "reddit_parse",
        "summary": "Drop galleries, videos, over_18; keep direct image URLs.",
        "audit": "pool_stats",
        "timeout_profile": "cpu_light",
    },
    {
        "phase_id": "FFV_P07_VIRALITY_RANK",
        "lane": "scoring",
        "summary": "Sort candidates by ViralityIndex (score + comments blend).",
        "audit": None,
        "timeout_profile": "cpu_light",
    },
    {
        "phase_id": "FFV_P08_WEIGHTED_SAMPLE",
        "lane": "rng",
        "summary": "Weighted pick with 2% decay per rank step (theory.txt).",
        "audit": "pick_weights",
        "timeout_profile": "cpu_light",
    },
    {
        "phase_id": "FFV_P09_EXCLUSION_REWRITE",
        "lane": "dedupe",
        "summary": "Remove already-used post ids from pool; widen if exhausted.",
        "audit": "dedupe_stats",
        "timeout_profile": "cpu_light",
    },
    {
        "phase_id": "FFV_P10_REACTION_POOL_MERGE",
        "lane": "reddit_io",
        "summary": "Fetch reaction-subreddit pools in parallel with reactable pass.",
        "audit": "pool_stats",
        "timeout_profile": "reddit",
    },
    {
        "phase_id": "FFV_P11_BLUEPRINT_MATERIALIZE",
        "lane": "plan",
        "summary": "Freeze SegmentBlueprint: reactable + reaction + sfx URL.",
        "audit": "blueprint",
        "timeout_profile": "instant",
    },
    {
        "phase_id": "FFV_P12_IMAGE_DOWNLOAD_REACTABLE",
        "lane": "binary_io",
        "summary": "Stream bytes for reactable still; detect HTML masquerading as image.",
        "audit": "bytes_meta",
        "timeout_profile": "media",
    },
    {
        "phase_id": "FFV_P13_IMAGE_DOWNLOAD_REACTION",
        "lane": "binary_io",
        "summary": "Stream bytes for reaction still; magic-byte sniff suffix.",
        "audit": "bytes_meta",
        "timeout_profile": "media",
    },
    {
        "phase_id": "FFV_P14_NORMALIZE_SUFFIX",
        "lane": "filesystem",
        "summary": "Rename to .png/.jpg/.webp so ffmpeg probes reliably.",
        "audit": None,
        "timeout_profile": "instant",
    },
    {
        "phase_id": "FFV_P15_ENCODE_REACTABLE_HOLD",
        "lane": "ffmpeg_video",
        "summary": f"Still → {REACTABLE_HOLD}s silent clip at target resolution (theory reactable hold).",
        "audit": "encode_timings",
        "timeout_profile": "ffmpeg_segment",
    },
    {
        "phase_id": "FFV_P16_ENCODE_REACTION_HOLD",
        "lane": "ffmpeg_video",
        "summary": f"Still → {REACTION_HOLD}s with 0.25s fade-in/out (theory reaction window).",
        "audit": "encode_timings",
        "timeout_profile": "ffmpeg_segment",
    },
    {
        "phase_id": "FFV_P17_CONCAT_VISUAL_CHAIN",
        "lane": "ffmpeg_mux",
        "summary": "Concat reactable clip + reaction clip (silent intermediate).",
        "audit": None,
        "timeout_profile": "ffmpeg_segment",
    },
    {
        "phase_id": "FFV_P18_SFX_RESOLVE_MYINSTANTS",
        "lane": "sfx",
        "summary": "Map MyInstants page URL to CDN /media/sounds/*.mp3 candidates.",
        "audit": "sfx_strategy",
        "timeout_profile": "short_io",
    },
    {
        "phase_id": "FFV_P19_SFX_BROWSER_HEADERS",
        "lane": "sfx",
        "summary": "Download audio with browser-like UA + Referer for CDN acceptance.",
        "audit": None,
        "timeout_profile": "media",
    },
    {
        "phase_id": "FFV_P20_SFX_YTDLP_FALLBACK",
        "lane": "sfx",
        "summary": "Optional yt-dlp extraction if direct CDN fails.",
        "audit": "sfx_strategy",
        "timeout_profile": "long_io",
    },
    {
        "phase_id": "FFV_P21_AUDIO_DELAY_MIX",
        "lane": "ffmpeg_audio",
        "summary": "Delay SFX to reaction window; anullsrc bed for full segment length.",
        "audit": None,
        "timeout_profile": "ffmpeg_segment",
    },
    {
        "phase_id": "FFV_P22_SEGMENT_FINALIZE",
        "lane": "filesystem",
        "summary": "Emit per-segment MP4 into work dir; ready for vertical concat.",
        "audit": "segment_done",
        "timeout_profile": "instant",
    },
    {
        "phase_id": "FFV_P23_VERTICAL_CONCAT_LIST",
        "lane": "ffmpeg_mux",
        "summary": "Demuxer concat N segments for 16:9 long-form output.",
        "audit": "concat_list",
        "timeout_profile": "ffmpeg_long",
    },
    {
        "phase_id": "FFV_P24_EXPORT_MOVE",
        "lane": "filesystem",
        "summary": "Move or finalize output under repo-root output/ with monotonic index.",
        "audit": "render_ok",
        "timeout_profile": "instant",
    },
    {
        "phase_id": "FFV_P25_WORKDIR_GC",
        "lane": "filesystem",
        "summary": "Best-effort delete ephemeral work directory.",
        "audit": None,
        "timeout_profile": "instant",
    },
    {
        "phase_id": "FFV_P26_AUDIT_APPEND",
        "lane": "telemetry",
        "summary": "Append JSONL audit record with UTC timestamp.",
        "audit": "always",
        "timeout_profile": "instant",
    },
    {
        "phase_id": "FFV_P27_DAG_SNAPSHOT",
        "lane": "telemetry",
        "summary": "Push DAG node timing snapshot into session state history.",
        "audit": "dag",
        "timeout_profile": "instant",
    },
    {
        "phase_id": "FFV_P28_INTERACTIVE_REPL",
        "lane": "ui",
        "summary": "Blocking CLI loop: short | video | seed | plan | status | ex.",
        "audit": None,
        "timeout_profile": "human",
    },
    {
        "phase_id": "FFV_P29_ERROR_RECOVERY",
        "lane": "resilience",
        "summary": "On failure: log audit, increment fail counter, preserve stderr context.",
        "audit": "render_fail",
        "timeout_profile": "instant",
    },
    {
        "phase_id": "FFV_P30_POOL_STALE_GUARD",
        "lane": "reddit_plan",
        "summary": "If pool too small after exclusions, relax dedupe to avoid deadlock.",
        "audit": "dedupe_relax",
        "timeout_profile": "instant",
    },
]

# ---------------------------------------------------------------------------
# Environment knobs (reference for operators)
# ---------------------------------------------------------------------------

FFV_ENV_CATALOG: list[tuple[str, str, str]] = [
    (
        "FFV_FUNNY",
        "0|1",
        "If 1, FFV starts in LOL preset (funny reactables + meme reactions + punchy SFX order).",
    ),
    (
        "FFV_USER_PACK",
        "path",
        "Folder of local .mp3/.wav/.m4a/.ogg/.flac files prepended to the SFX pool (absolute, ~, or cwd-relative).",
    ),
    ("FFV_UA", "string", "Override Reddit HTTP User-Agent (falls back to PTK_UA)."),
    ("PTK_UA", "string", "Shared UA when FFV_UA unset."),
    ("FFV_REDDIT_PAUSE", "seconds", "Sleep between Reddit JSON requests (default 0.25)."),
    ("FFV_REDDIT_JSON_ATTEMPTS", "int", "Retries per listing (hosts × UAs × attempts; default 4, max 12)."),
    ("FFV_DRY_RUN", "0|1", "If 1, skip ffmpeg/render; still plans and audits dry-run flag."),
    ("FFV_VERBOSE_PHASES", "0|1", "Print phase catalog hints during long renders."),
    ("FFV_SFX_DIVERSITY", "0.0-1.0", "Bias SFX picks away from recently used families + URLs (default 0.52)."),
    ("FFV_SFX_URL_LOOKBACK", "int", "How many recent SFX URLs to penalize repeating (default 10)."),
    ("FFV_SFX_CONTEXT", "0|1", "Context-aware SFX ranking from subreddit + post titles (default 1)."),
    ("FFV_SFX_CONTEXT_STRENGTH", "0.0-1.0", "How strongly context steering affects family selection (default 0.55)."),
    ("FFV_FORCE_SFX_URL", "url", "Debug: pin every segment to one MyInstants page URL."),
    ("FFV_VIDEO_INNER_XFADE", "seconds", "`video` mode only: crossfade react→reaction (0 disables; default 0.18)."),
    ("FFV_VIDEO_OUTER_XFADE", "seconds", "Between `video` segments (0 disables; default 0.14)."),
    ("FFV_VIDEO_MOTION", "0|1", "`video` mode only: Ken Burns–style drift on stills (default 1)."),
    ("FFV_VIDEO_GRADE", "0|1", "`video` mode only: color grades (pools + presets; default 1)."),
    ("FFV_VIDEO_VIGNETTE", "0|1", "`video` / `short`+FFV_SHORT_LOOK: soft edge darkening (default 1 for video)."),
    ("FFV_VIDEO_SHARPEN", "0|1", "Occasional mild unsharp after grade (default 1 for video)."),
    ("FFV_VIDEO_GRAIN", "0|1", "Rare subtle film grain (default 0)."),
    ("FFV_VIDEO_CHYRON", "0|1", "`video` mode: r/sub + post title overlay (default 1)."),
    ("FFV_VIDEO_FONT", "path", "TTF for chyrons (default: Windows Arial / leave unset for drawtext default)."),
    ("FFV_SHORT_LOOK", "0|1", "Apply video-style look (vignette/sharpen pools) to `short` renders."),
    ("FFV_SFX_MIX_LEVEL", "0.0-1.0", "MyInstants SFX volume in mix (default 0.88)."),
    ("FFV_SFX_AFADE", "0|1", "Tiny fades on SFX in/out in the reaction window (default 1)."),
    ("FFV_VIDEO_TRANSITIONS", "csv", "Override xfade names, e.g. fade,wipeleft,slideright."),
    ("FFV_SKIP_IMAGE_REENCODE", "0|1", "Skip all image demux (fastest; may break lying .png URLs)."),
    ("FFV_ALWAYS_IMAGE_DEMUX", "0|1", "Always run ffmpeg demux-to-JPEG (redundant now except for GIF; debug)."),
    ("FFV_FFMPEG_PRESET", "name", "ffv_default (fast) | ffv_quick | ffv_quality | ffv_premium | ffv_fast."),
    ("FFV_FFMPEG_THREADS", "int", "ffmpeg -threads (0=auto per codec; empty=omit flag)."),
    ("FFV_FFMPEG_VERBOSE", "0|1", "Send ffmpeg stderr to console (default off; avoids parallel spam)."),
    ("FFV_ZOOMPAN_MAX_W", "pixels", "Cap Ken Burns upscale width (default 2880; lower = faster `video` encodes)."),
    ("FFV_SEGMENT_WORKERS", "int", "Parallel `video` segment encodes (0=auto≈min(6,⌈CPU/2⌉); cap 10; 1=serial)."),
    ("FFV_SEGMENT_SERIAL", "flag", "Force serial segment encodes (same as FFV_SEGMENT_WORKERS=1)."),
    ("FFV_POOL_WORKERS", "int", "Parallel Reddit sub fetches per pool (0=auto; 1=serial)."),
]

# ---------------------------------------------------------------------------
# SFX slug → tag families (keyword inference)
# ---------------------------------------------------------------------------

_SFX_KEYWORD_BUCKETS: list[tuple[FrozenSet[str], str]] = [
    (frozenset({"vine", "boom", "bruh", "pluh", "womp", "dun"}), "impact_meme"),
    (frozenset({"fart", "toilet", "diarrhea", "poop"}), "grossout_comedy"),
    (frozenset({"anime", "wow", "ahh", "nellie", "moe"}), "anime_reaction"),
    (frozenset({"metal", "pipe", "clang", "gear", "solid"}), "industrial_hit"),
    (frozenset({"mario", "roblox", "fnaf", "spongebob", "undertaker"}), "gaming_toon"),
    (frozenset({"violin", "sad", "meow", "emotional", "damage"}), "sad_stinger"),
    (frozenset({"horn", "mlg", "air", "yeah", "rizz", "kanye"}), "hype_stinger"),
    (frozenset({"discord", "notification", "phone", "ringing"}), "digital_ping"),
    (frozenset({"gunshot", "punch", "whip", "hit", "bonk", "slap"}), "percussion_hit"),
    (frozenset({"x-files", "prowler", "siren", "credit"}), "dramatic_sting"),
    (frozenset({"charlie", "kirk", "skibidi", "italian", "brainrot"}), "ironic_voice"),
    (frozenset({"error", "beep", "censor", "smoke"}), "alarm_tech"),
    (frozenset({"oof", "minecraft"}), "gaming_toon"),
    (frozenset({"windows", "xp", "startup", "shutdown"}), "digital_ping"),
    (frozenset({"coffin", "dance", "astronomia"}), "dramatic_sting"),
    (frozenset({"quack", "duck", "goose"}), "grossout_comedy"),
    (frozenset({"burp", "scratch", "yeet"}), "impact_meme"),
]

# Merged after theory.txt URLs (deduped). Strengthens variety when theory is thin or picks collapse.
FFV_BONUS_SFX_URLS: tuple[str, ...] = (
    "https://www.myinstants.com/en/instant/vine-boom-sound-70972/",
    "https://www.myinstants.com/en/instant/bruh/",
    "https://www.myinstants.com/en/instant/metal-pipe-clang-80894/",
    "https://www.myinstants.com/en/instant/galaxy-meme-18643/",
    "https://www.myinstants.com/en/instant/the-weeknd-rizzz-2710/",
    "https://www.myinstants.com/en/instant/fahhhhhhhhhhhhhh-3525/",
    "https://www.myinstants.com/en/instant/rip-my-granny-loud-asf-56750/",
    "https://www.myinstants.com/en/instant/we-are-charlie-kirk-phone-95091/",
    "https://www.myinstants.com/en/instant/bad-to-the-bone-meme-22189/",
    "https://www.myinstants.com/en/instant/epstein-sound-27365/",
    "https://www.myinstants.com/en/instant/flashbang-gah-dayum-64535/",
    "https://www.myinstants.com/en/instant/we-do-not-care-tiktok-sound-45123/",
    "https://www.myinstants.com/en/instant/kim-jong-un-is-a-master-of-goon-10790/",
    "https://www.myinstants.com/en/instant/defy-gravity-x-god-is-kanye-67375/",
    "https://www.myinstants.com/en/instant/x-files/",
    "https://www.myinstants.com/en/instant/67-71609/",
    "https://www.myinstants.com/en/instant/999-social-credit-siren-82729/",
    "https://www.myinstants.com/en/instant/a-few-moments-later-sponge-bob-sfx-fun-80331/",
    "https://www.myinstants.com/en/instant/a-ty-zhirnaia-32879/",
    "https://www.myinstants.com/en/instant/ack-87763/",
    "https://www.myinstants.com/en/instant/among-us-role-reveal-sound-34956/",
    "https://www.myinstants.com/en/instant/anderdingus-64368/",
    "https://www.myinstants.com/en/instant/mlg-air-horn-3718/",
    "https://www.myinstants.com/en/instant/discord-notification-38119/",
    "https://www.myinstants.com/en/instant/windows-xp-error-42042/",
    "https://www.myinstants.com/en/instant/roblox-oof-43192/",
    "https://www.myinstants.com/en/instant/anime-wow-sound-89672/",
    "https://www.myinstants.com/en/instant/sad-violin-the-meme-one-5327/",
    "https://www.myinstants.com/en/instant/to-be-continued-jojo-7117/",
    "https://www.myinstants.com/en/instant/emotional-damage-meme-82729/",
)


def sfx_slug_from_url(url: str) -> str:
    if not (url.startswith("http://") or url.startswith("https://")):
        try:
            stem = Path(url).stem
            if stem:
                return stem[:120]
        except (OSError, ValueError):
            pass
        return "local_file"
    try:
        path = url.split("?", 1)[0].rstrip("/")
        parts = [p for p in path.split("/") if p]
        for i, p in enumerate(parts):
            if p.lower() == "instant" and i + 1 < len(parts):
                return parts[i + 1]
    except (ValueError, IndexError):
        pass
    return "unknown"


def sfx_infer_families(slug: str) -> frozenset[str]:
    s = slug.lower()
    tags: set[str] = {"generic"}
    for keys, fam in _SFX_KEYWORD_BUCKETS:
        for k in keys:
            if k in s:
                tags.add(fam)
                break
    if re.search(r"\d{3,}", s):
        tags.add("numeric_slug")
    return frozenset(tags)


FFV_SFX_MUSIC_SLUG_EXACT = frozenset(
    {
        "bad-to-the-bone-meme-22189",
        "coffin-dance",
        "astronomia",
        "to-be-continued-jojo-7117",
        "defy-gravity-x-god-is-kanye-67375",
        "we-do-not-care-tiktok-sound-45123",
        "rip-my-granny-loud-asf-56750",
    }
)
FFV_SFX_MUSIC_SLUG_HINTS = (
    "song",
    "music",
    "beat",
    "remix",
    "phonk",
    "lofi",
    "bass-boost",
    "spotify",
    "tiktok-sound",
    "kanye",
    "weeknd",
    "drake",
    "travis",
    "eminem",
    "jojo",
    "astronomia",
    "bad-to-the-bone",
)


def sfx_is_music_like_url(url: str) -> bool:
    slug = sfx_slug_from_url(url).lower()
    if slug in FFV_SFX_MUSIC_SLUG_EXACT:
        return True
    return any(h in slug for h in FFV_SFX_MUSIC_SLUG_HINTS)


def format_phase_deck(limit: int | None = None) -> str:
    lines: list[str] = []
    for i, ph in enumerate(FFV_RENDER_PHASES if limit is None else FFV_RENDER_PHASES[:limit]):
        pid = ph.get("phase_id", "?")
        lane = ph.get("lane", "?")
        summ = ph.get("summary", "")
        lines.append(f"{i:02d}  {pid}  [{lane}]  {summ}")
    return "\n".join(lines)


def subreddit_diversity_hint(sub: str) -> dict[str, Any] | None:
    return REACTABLE_CLUSTER.get(sub) or REACTION_CLUSTER.get(sub)


def catalog_digest() -> str:
    return (
        f"reactable_clusters={len(REACTABLE_CLUSTER)}|"
        f"reaction_clusters={len(REACTION_CLUSTER)}|"
        f"phases={len(FFV_RENDER_PHASES)}|env_keys={len(FFV_ENV_CATALOG)}"
    )


def iter_theory_sfx_urls(text: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"https://www\.myinstants\.com/[^\s]+", text, flags=re.I):
        u = m.group(0).strip().split()[0]
        out.append(u)
    return list(dict.fromkeys(out))


def load_sfx_rows_from_theory(path: Path) -> list[tuple[str, str, frozenset[str]]]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    rows: list[tuple[str, str, frozenset[str]]] = []
    for u in iter_theory_sfx_urls(text):
        slug = sfx_slug_from_url(u)
        rows.append((u, slug, sfx_infer_families(slug)))
    return rows


# ---------------------------------------------------------------------------
# FFmpeg tuning profiles (selected by env FFV_FFMPEG_PRESET or engine default)
# ---------------------------------------------------------------------------

FFV_FFMPEG_TUNING_PROFILES: dict[str, dict[str, Any]] = {
    # Default: speed-friendly; use ffv_quality / ffv_premium for slower prettier encodes.
    "ffv_default": {
        "video_codec": "libx264",
        "preset": "veryfast",
        "crf": "21",
        "x264_params": "aq-mode=2:me=hex:subme=6:ref=3",
        "pix_fmt": "yuv420p",
        "audio_codec": "aac",
        "audio_bitrate": "192k",
        "still_fps": 24,
        "scale_flags": "lanczos",
    },
    "ffv_quick": {
        "video_codec": "libx264",
        "preset": "superfast",
        "crf": "24",
        "pix_fmt": "yuv420p",
        "audio_codec": "aac",
        "audio_bitrate": "160k",
        "still_fps": 24,
        "scale_flags": "bicubic",
    },
    "ffv_quality": {
        "video_codec": "libx264",
        "preset": "medium",
        "crf": "19",
        "tune": "film",
        "x264_params": "aq-mode=3:aq-strength=1.0:me=umh:subme=9:ref=5",
        "pix_fmt": "yuv420p",
        "audio_codec": "aac",
        "audio_bitrate": "256k",
        "still_fps": 30,
        "scale_flags": "lanczos+accurate_rnd+full_chroma_int",
    },
    "ffv_premium": {
        "video_codec": "libx264",
        "preset": "slow",
        "crf": "17",
        "tune": "film",
        "x264_params": "aq-mode=3:aq-strength=1.05:me=umh:subme=10:ref=6:bframes=6",
        "pix_fmt": "yuv420p",
        "audio_codec": "aac",
        "audio_bitrate": "320k",
        "still_fps": 30,
        "scale_flags": "lanczos+accurate_rnd+full_chroma_int",
    },
    "ffv_fast": {
        "video_codec": "libx264",
        "preset": "ultrafast",
        "crf": "26",
        "pix_fmt": "yuv420p",
        "audio_codec": "aac",
        "audio_bitrate": "128k",
        "still_fps": 24,
        "scale_flags": "fast_bilinear",
    },
    "ffv_concat_copy": {
        "demuxer": "concat",
        "copy_video": True,
        "copy_audio": True,
        "fallback_reencode": True,
    },
}

# ---------------------------------------------------------------------------
# Rank-decay calibration (documentation + optional future non-linear curves)
# ---------------------------------------------------------------------------

RANK_DECAY_MODELS: dict[str, dict[str, float]] = {
    "theory_linear_2pct": {"base": 1.0, "per_rank": 0.02, "floor": 0.005},
    "gentle_1pct": {"base": 1.0, "per_rank": 0.01, "floor": 0.01},
    "aggressive_4pct": {"base": 1.0, "per_rank": 0.04, "floor": 0.002},
}

# ---------------------------------------------------------------------------
# Operator-facing error code registry (UI may surface these verbatim)
# ---------------------------------------------------------------------------

FFV_ERROR_CODES: list[tuple[str, str, str]] = [
    ("FFV-E001", "pool", "Reactable pool below minimum after fetch + filter."),
    ("FFV-E002", "pool", "Reaction image pool below minimum after fetch + filter."),
    ("FFV-E003", "network", "Reddit JSON request failed or returned non-200."),
    ("FFV-E004", "network", "Image bytes download failed or too small."),
    ("FFV-E005", "media", "Downloaded body is HTML/XML instead of raster image."),
    ("FFV-E006", "media", "Magic bytes do not match png/jpeg/webp/gif/bmp."),
    ("FFV-E007", "ffmpeg", "Still encode for reactable hold failed."),
    ("FFV-E008", "ffmpeg", "Still encode for reaction hold + fades failed."),
    ("FFV-E009", "ffmpeg", "Concat of silent clips failed."),
    ("FFV-E010", "sfx", "All MyInstants CDN candidates and yt-dlp fallbacks failed."),
    ("FFV-E011", "ffmpeg", "Audio delay + mix graph failed."),
    ("FFV-E012", "ffmpeg", "Vertical concat demuxer failed even after reencode."),
    ("FFV-E013", "theory", "theory.txt missing and embedded corpus empty."),
    ("FFV-E014", "dedupe", "Exclusion sets exhausted entire pool (should auto-relax)."),
    ("FFV-E015", "session", "Audit log directory not writable."),
    ("FFV-E016", "rng", "Invalid seed string (non-integer)."),
    ("FFV-E017", "ui", "Unknown interactive command."),
    ("FFV-E018", "plan", "Dry-run planning could not reach Reddit."),
    ("FFV-E019", "sfx", "FFV_FORCE_SFX_URL invalid or fetch failed."),
    ("FFV-E020", "config", "Video segment count out of 10–30 theory band."),
]

# ---------------------------------------------------------------------------
# Cross-phase dependency edges (for `help deep` graph imagination)
# ---------------------------------------------------------------------------

FFV_PHASE_EDGES: list[tuple[str, str, str]] = [
    ("FFV_P00_SESSION_ALLOC", "FFV_P01_CORPUS_LOAD", "needs_paths"),
    ("FFV_P01_CORPUS_LOAD", "FFV_P02_SEED_HYDRATE", "rng_order"),
    ("FFV_P02_SEED_HYDRATE", "FFV_P03_SUB_SAMPLE_PLAN", "deterministic_subsample"),
    ("FFV_P03_SUB_SAMPLE_PLAN", "FFV_P05_HOT_JSON_FETCH", "reddit_io"),
    ("FFV_P05_HOT_JSON_FETCH", "FFV_P06_CHILD_FILTER_IMAGE", "json_parse"),
    ("FFV_P06_CHILD_FILTER_IMAGE", "FFV_P07_VIRALITY_RANK", "score_sort"),
    ("FFV_P07_VIRALITY_RANK", "FFV_P08_WEIGHTED_SAMPLE", "weighted_choice"),
    ("FFV_P08_WEIGHTED_SAMPLE", "FFV_P09_EXCLUSION_REWRITE", "dedupe_optional"),
    ("FFV_P09_EXCLUSION_REWRITE", "FFV_P11_BLUEPRINT_MATERIALIZE", "freeze_bp"),
    ("FFV_P11_BLUEPRINT_MATERIALIZE", "FFV_P12_IMAGE_DOWNLOAD_REACTABLE", "bytes"),
    ("FFV_P12_IMAGE_DOWNLOAD_REACTABLE", "FFV_P14_NORMALIZE_SUFFIX", "probe"),
    ("FFV_P14_NORMALIZE_SUFFIX", "FFV_P15_ENCODE_REACTABLE_HOLD", "ffmpeg"),
    ("FFV_P15_ENCODE_REACTABLE_HOLD", "FFV_P16_ENCODE_REACTION_HOLD", "ffmpeg_chain"),
    ("FFV_P16_ENCODE_REACTION_HOLD", "FFV_P17_CONCAT_VISUAL_CHAIN", "mux"),
    ("FFV_P17_CONCAT_VISUAL_CHAIN", "FFV_P18_SFX_RESOLVE_MYINSTANTS", "parallel_ok"),
    ("FFV_P18_SFX_RESOLVE_MYINSTANTS", "FFV_P21_AUDIO_DELAY_MIX", "audio_graph"),
    ("FFV_P21_AUDIO_DELAY_MIX", "FFV_P22_SEGMENT_FINALIZE", "segment_mp4"),
    ("FFV_P22_SEGMENT_FINALIZE", "FFV_P23_VERTICAL_CONCAT_LIST", "only_multi_segment"),
    ("FFV_P23_VERTICAL_CONCAT_LIST", "FFV_P24_EXPORT_MOVE", "output_index"),
    ("FFV_P24_EXPORT_MOVE", "FFV_P25_WORKDIR_GC", "cleanup"),
    ("FFV_P25_WORKDIR_GC", "FFV_P26_AUDIT_APPEND", "telemetry"),
    ("FFV_P29_ERROR_RECOVERY", "FFV_P26_AUDIT_APPEND", "fail_audit"),
]

# ---------------------------------------------------------------------------
# SFX family → suggested pairing with reactable archetypes (soft hints only)
# ---------------------------------------------------------------------------

SFX_FAMILY_ARCHETYPE_AFFINITY: dict[str, frozenset[str]] = {
    "impact_meme": frozenset(
        {
            "predictable_disaster",
            "whiplash_cut",
            "immediate_consequence",
            "traffic_absurdism",
        }
    ),
    "sad_stinger": frozenset(
        {
            "mundane_catastrophe",
            "emotional_collapse_micro",
            "effort_without_payoff",
        }
    ),
    "anime_reaction": frozenset(
        {
            "pseudo_educational_meme",
            "linguistic_glitch",
            "genre_bend",
        }
    ),
    "industrial_hit": frozenset(
        {
            "design_fail",
            "malicious_compliance_ui",
            "great_execution_bad_taste",
        }
    ),
    "grossout_comedy": frozenset(
        {
            "hair_and_image_disaster",
            "translation_artifact",
            "cursed_stock_photo",
        }
    ),
    "hype_stinger": frozenset(
        {
            "stupid_game_stupid_prize",
            "stunt_fail",
            "kinetics",
        }
    ),
    "digital_ping": frozenset(
        {
            "human_decision_fail",
            "hostile_ux",
            "screenshots",
        }
    ),
    "dramatic_sting": frozenset(
        {
            "liminal_uncanny",
            "visual_paradox",
            "delayed_realization",
        }
    ),
    "ironic_voice": frozenset(
        {
            "british_panel_chaos",
            "ironic_advice",
            "witness_reaction_in_frame",
        }
    ),
    "alarm_tech": frozenset(
        {
            "dark_patterns",
            "software",
            "objects",
        }
    ),
    "generic": frozenset({"*"}),
}


def archetype_for_sub(sub: str) -> str | None:
    row = subreddit_diversity_hint(sub)
    if not row:
        return None
    return str(row.get("archetype") or "")


def sfx_families_compatible_with_sub(families: Iterable[str], sub: str) -> float:
    """Return 0..1 soft score; engine uses as tie-break only."""
    arch = archetype_for_sub(sub)
    if not arch:
        return 0.5
    score = 0.45
    for fam in families:
        aff = SFX_FAMILY_ARCHETYPE_AFFINITY.get(fam)
        if not aff:
            continue
        if "*" in aff or arch in aff:
            score += 0.09
    return min(1.0, score)


# ---------------------------------------------------------------------------
# Storyboard templates (seconds) — mirrors theory segment timeline
# ---------------------------------------------------------------------------

FFV_SEGMENT_TIMELINE: list[dict[str, Any]] = [
    {"t0": 0.0, "t1": REACTABLE_HOLD, "track": "reactable", "audio": "silent", "label": "Hold reddit still (short 9:16 or video 16:9 canvas)."},
    {
        "t0": REACTABLE_HOLD,
        "t1": SEGMENT_VISUAL_TOTAL,
        "track": "reaction",
        "audio": "sfx_delayed",
        "label": "Reaction still with fades; SFX muxed after reactable hold.",
    },
]


FFV_QUALITY_GATES: list[tuple[str, str]] = [
    ("QG-01", "At least N distinct image posts per pool before planning."),
    ("QG-02", "Exclude post ids must not empty entire pool without relax."),
    ("QG-03", "Downloaded raster must pass magic-byte sniff."),
    ("QG-04", "FFmpeg segment duration must match REACTABLE_HOLD + REACTION_HOLD."),
    ("QG-05", "SFX trim window must match REACTION_HOLD for atrim branch."),
    ("QG-06", "Video (multi-segment) compile must concatenate same WxH per segment."),
    ("QG-07", "Audit JSONL append must be atomic per line."),
    ("QG-08", "Session workdir must be removed in finally block."),
]

FFV_INTERACTIVE_COMMANDS: list[tuple[str, str]] = [
    ("short", "Render one 1080×1920 (9:16) classic reaction segment."),
    ("video", "Render 1920×1080 (16:9) compilation with motion, inner/outer xfade, linked audio; video N = segment count."),
    ("seed N", "Deterministic RNG for picks."),
    ("plan", "Fetch pools and print one sample blueprint (no ffmpeg)."),
    ("status", "Session counters, corpus sizes, last DAG snapshot."),
    ("catalog", "Print static digest: phases, errors, ffmpeg profiles, env keys."),
    ("deep", "Abridged phase deck + dependency sample."),
    ("help", "Reprint command deck."),
    ("ex", "Return to main tool menu."),
]


def segment_timeline_human() -> str:
    lines = [f"{row['t0']:.2f}–{row['t1']:.2f}s  {row['track']}: {row['label']}" for row in FFV_SEGMENT_TIMELINE]
    return "\n".join(lines)
