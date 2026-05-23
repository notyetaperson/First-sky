"""
Gen-Z / teen short-form script generators (12–17 skew).

Inspired by conversational formats: wrong-answers-only, POV brain, Yelp-style ratings,
one-object stories, honest settings parody, fake museum plaques, silent-caption scripts,
speedrun-style commentary over a school day, and petty “small creator wronged me” storytime.
No Reddit fetch — template + word pools only.
"""
from __future__ import annotations

import random

# -----------------------------------------------------------------------------
# Shared pools
# -----------------------------------------------------------------------------

_MICRO_TOPICS: tuple[str, ...] = (
    "the teacher says pick a partner and you don't know anyone",
    "your crush's name appears while typing in the group chat",
    "the bus is late and your phone is on four percent",
    "the Wi-Fi drops right before you submit the assignment",
    "someone asks what music you listen to and your playlist is unhinged",
    "you walk into class and everyone is already seated in a new seating chart",
    "the vending machine eats your dollar but doesn't drop the snack",
    "your mom calls you mid-game and you have to sound responsible",
    "the substitute teacher mispronounces your name confidently",
    "you realize you replied all on the school email thread",
)

_WRONG_TOPICS: tuple[str, ...] = (
    "how to survive lunch period",
    "what to do when the group project carries you",
    "how to look busy when you forgot the homework",
    "how to exit a boring FaceTime politely",
    "what to say when someone asks if you're okay and you're not",
    "how to pretend you understand the math problem",
    "how to recover after sending a screenshot to the wrong person",
)

_OBJECTS: tuple[str, ...] = (
    "a single hoodie tied around your waist",
    "a cracked phone charger that still works if you hold it right",
    "a hall pass that's seen things",
    "one airpod, survivor edition",
    "the last good pen in the house",
    "a water bottle with seventeen stickers",
)

_RATE_TARGETS: tuple[str, ...] = (
    "Monday mornings",
    "the school cafeteria pizza",
    "fire drills during a test",
    "group projects where one person does everything",
    "the school Wi-Fi",
    "assemblies that could have been an email",
    "tryouts where you definitely practiced the wrong thing",
)

_FAKE_APPS: tuple[str, ...] = (
    "Homework.exe",
    "GroupChat Chaos™",
    "Crush Simulator (buggy build)",
    "School Brain OS",
    "Sleep Schedule (deprecated)",
    "Motivation™ (trial expired)",
)

_MUSEUM_SUBJECTS: tuple[str, ...] = (
    "the phrase no cap",
    "the ritual of air-dropping memes in the library",
    "the study hack that is just crying first",
    "the group chat name that makes no sense to outsiders",
    "the hallway speed-walk when you're late",
    "the fake yawn when you want to leave a conversation",
)


def _pick(seq: tuple[str, ...]) -> str:
    return random.choice(seq)


def script_wrong_answers_only() -> str:
    """Wrong answers only — question + three escalating absurd wrong answers."""
    topic = _pick(_WRONG_TOPICS)
    a1 = "Do it confidently and hope nobody checks."
    a2 = "Start a rumor that the assignment was optional."
    a3 = "Time travel, but only backward by one class period."
    return (
        f"Wrong answers only. Here's the question: how do you handle {topic}? "
        f"First wrong answer: {a1} "
        f"Second, worse answer: {a2} "
        f"Final boss wrong answer: {a3} "
        "Comment which one is accidentally kind of valid."
    )


def script_pov_brain() -> str:
    """Hyper-specific POV internal monologue."""
    moment = _pick(_MICRO_TOPICS)
    beats = [
        "Brain: act normal. You: forgets how to walk.",
        "You're calculating escape routes like it's a heist movie.",
        "You rehearse one sentence seventeen times and still say the wrong thing.",
        "Time slows down. Your inner narrator turns on captions.",
        "You pretend to check your phone. The phone is at one percent. Legend behavior.",
    ]
    random.shuffle(beats)
    body = " ".join(beats[:3])
    return f"P O V: {moment}. {body} If you felt that, you're not alone."


def script_rating_review() -> str:
    """Yelp-style rating of a relatable thing."""
    thing = _pick(_RATE_TARGETS)
    stars = random.choice([2, 3, 4])
    pros = random.choice(
        [
            "Pros: builds character, allegedly.",
            "Pros: free drama, great stories later.",
            "Pros: you learn patience. Or rage. Same thing.",
        ]
    )
    cons = random.choice(
        [
            "Cons: zero stars for timing. Who approved this timeline.",
            "Cons: the vibes are inconsistent patch notes.",
            "Cons: would not recommend to my past self.",
        ]
    )
    return (
        f"Rating {thing} like it's a restaurant review. "
        f"I give it {stars} out of five stars. {pros} {cons} "
        "Tag someone who needs this review."
    )


def script_one_object_story() -> str:
    """One-object storytelling constraint."""
    obj = _pick(_OBJECTS)
    arc = random.choice(
        [
            "It started as backup. It became the main character.",
            "Everyone underestimated it. It saw everything.",
            "It doesn't judge. It just witnesses your choices.",
            "Three years of history in one object. No notes, just vibes.",
        ]
    )
    return (
        f"One object storytelling challenge. The object: {obj}. {arc} "
        "If this object could talk, it would file a complaint — respectfully."
    )


def script_honest_settings() -> str:
    """Parody honest settings toggles."""
    app = _pick(_FAKE_APPS)
    toggles = random.sample(
        [
            "Read receipts: permanently on for enemies, off for peace.",
            "Homework reminders: ignored with style.",
            "Motivation notifications: snoozed until graduation.",
            "Sleep mode: theoretical.",
            "Procrastination boost: enabled by default.",
            "Main character energy: unstable build.",
            "Cringe buffer: loading forever.",
        ],
        k=min(5, 7),
    )
    body = "".join(f"Setting: {t}. " for t in toggles)
    return f"If {app} had honest settings. {body} Save this before the patch notes drop."


def script_museum_2045() -> str:
    """Fake museum plaque about current teen culture."""
    subj = _pick(_MUSEUM_SUBJECTS)
    era = random.choice(["Early Zoom School Era", "Peak Meme Acceleration Period", "Pre-AI Homework Limbo"])
    return (
        f"Gen Z museum exhibit, year twenty forty five. Title: {subj}. "
        f"Era: {era}. "
        "Plaque text: Scholars believe participants performed this ritual to signal belonging, "
        "avoid eye contact, and farm likes simultaneously. "
        "Do not touch the artifact. It still has notifications on."
    )


def script_silent_captions() -> str:
    """Silent skit — TTS as 'director' reading caption cards + stage beats."""
    scenes = [
        "[On screen — no talking] You freeze. The teacher makes eye contact.",
        "[Caption] me pretending I totally heard the instructions",
        "[On screen] slow nod. zero comprehension.",
        "[Caption] inner scream, outer chill",
        "[On screen] phone brightness at minimum. stealth mode engaged.",
    ]
    random.shuffle(scenes)
    picked = scenes[:4]
    return (
        "Silent skit energy — captions only, watch without sound if you have to. "
        + " ".join(picked)
        + " If you lived this, like without making eye contact with your screen."
    )


_SMALL_CREATOR_NICHES: tuple[str, ...] = (
    "a Minecraft Shorts account that only posts cliffhangers",
    "a study-tok channel that color-codes anxiety",
    "a Roblox edit account with suspiciously clean transitions",
    "a slime ASMR page that whispers drama in the captions",
    "a vlog-style account that films their ceiling when they're mad",
    "a reaction channel that pauses on your face like it's evidence",
    "a 'day in my life' account where the day is always chaotic neutral",
    "a ranking channel that ranks everything except accountability",
    "a 'motivation' account that posts screenshots of strangers losing their temper",
    "a podcast clip account that takes jokes out of context on purpose",
    "a 'toxic trait' account that farms shame and calls it content",
    "a GRWM account that starts crying when the views dip",
    "a 'truth' account that speaks in absolutes and never cites sources",
    "a storytime voice that sounds innocent but the script is a knife",
    "a 'small business' flex account that bullies other small businesses",
    "a reply guy turned creator with a god complex and a ring light",
)

# Mostly pretty rough petty-creator / platform-drama beats (satire, fictional).
_SMALL_CREATOR_WRONGS: tuple[str, ...] = (
    "left a paragraph in my comments like they're the protagonist of my video",
    "stitched my clip upside down and added sad trombone for no reason",
    "said my take was mid, then posted the same take two days later with better lighting",
    "read my community post wrong on purpose so their reply would go viral",
    "subtweeted with my exact phrase in quotes like it's a museum exhibit",
    "used my sound and acted like they invented the trend",
    "duetted me and just stared in silence for seven seconds — psychological warfare",
    "commented who asked on a video that was literally answering a question",
    "ratio'd me with a screenshot of my own profile like that's a flex",
    "replied cry about it then blocked me before I could say anything normal",
    "put my face in the thumbnail zoomed like I'm crying when I was literally blinking",
    "opened with no hate and then wrote three paragraphs of hate-shaped opinions",
    "called me kid like we're not the same age and the same semester",
    "quoted my post with only a skull emoji like that's a closing argument",
    "left a timestamp comment that was wrong on purpose so people would argue under my video",
    "made a 'storytime' about 'someone' that used my exact wording from last week",
    "pinned their own comment under my video like they own the replies",
    "said I'm not calling anyone out while describing my outfit and my hair part",
    "reuploaded my edit with a filter and slower speed like it's a remix",
    "told their Discord I was 'obsessed' because I defended myself once",
    "faked a DM screenshot where I 'said something unhinged' — I never typed that",
    "sent their followers to flood my comments with the same copy-paste insult",
    "accused me of copying them when their upload date was two days after mine",
    "made a whole video 'reacting' but it was just them smirking at my face",
    "left a one-star review on a playlist that wasn't even mine",
    "commented touch grass under a video where I was literally outside",
    "said I look like I argue in grocery store parking lots — I've never even driven there",
    "turned my joke into their merch slogan and acted brand-blind",
    "claimed I 'stole clout' from a trend I literally started in our school group chat",
    "posted a 'mental health check' video using my face as the 'bad example' freeze-frame",
    "told people I 'love drama' because I asked them to stop lying about me",
    "edited my voice to sound meaner and added villain music",
    "made a 'who wore it better' with my yearbook photo and a cartoon raccoon",
    "said I give 'pick me' energy for saying please and thank you",
    "left a comment like 'prayers' like I'm a disaster relief zone",
    "asked their chat to mass-report my video for harassment — they started it",
    "quoted me out of order so it looks like I admitted to something insane",
    "said I'm 'chronically online' from an account that posts seventeen times a day",
    "replied to my apology with 'skill issue' like morality is a game mechanic",
    "made a tier list and put me in F tier next to 'wet socks'",
    "used my trauma-adjacent joke as a punchline in their sponsor segment",
    "said I 'need therapy' like it's a dunk, not a human need",
    "left a heart emoji under hate so the algorithm blesses the cruelty",
    "posted 'I don't do drama' then uploaded a twelve-part series about me",
    "claimed I 'faked' being busy because I didn't respond in four minutes",
    "said my voice is 'giving NPC' because I was monotone from being sick",
    "cropped my face next to a clown and called it 'educational content'",
    "told everyone I 'rage quit' a collab I never agreed to",
    "made a 'boundaries' post that was basically a subtweet with my initials",
    "said I 'weaponized the block button' for blocking spam accounts",
    "reposted my art without credit until it blew up, then added credit in tiny text",
    "accused me of 'clout chasing' for replying to their public callout",
    "said my content is 'for kids' like that's an insult and a legal threat",
    "left 'you good?' under a video where I'm clearly venting — performative concern",
    "made a poll: 'is OP lying?' with no context and let strangers vote on my character",
    "said I 'love attention' because I turned comments on",
    "compared me to a scam ad because I used a jump cut",
    "told mutuals I'm 'unsafe' with zero receipts — just vibes and a ring light",
    "used my face as a reaction image in their community tab for 'red flags'",
    "said I 'gaslight' because I corrected a fact they invented",
    "claimed I 'doxxed' them because I said their public username out loud",
    "made a 'lesson learned' video where the lesson is that I'm the villain",
    "said I 'can't take a joke' about a joke that was literally my trauma",
    "posted 'be kind' then liked every mean reply under my video",
    "said I'm 'desperate' for posting twice a week while they post hourly",
    "turned my typo into a meme and pretended it was my whole personality",
    "left 'who hurt you' like they're a therapist and not the person hurting me",
    "made a 'hot take' that was just insulting my appearance for three minutes",
    "said I 'copy edits' because we both used a trending transition once",
    "accused me of buying followers because I had a good day on the FYP",
    "replied 'cope' to me setting a boundary like it's philosophy",
    "said I 'love victimhood' because I asked them to delete a lie",
    "made a 'debunk' video that debunked something I never claimed",
    "posted my old clip from middle school like it's a smoking gun",
    "said I 'talk too much' in a video where they talked over my audio",
    "used my screenshot in a 'toxic traits' slideshow without blurring my name",
    "claimed I 'started a witch hunt' because two friends defended me",
    "said I'm 'performative' for crying on camera then mocked me for not crying",
    "left 'seek help' under a video about my dog dying — pure evil timing",
    "made a 'I'm not mad' live where they were absolutely mad for an hour",
    "said I 'fake niceness' because I said hi in a collab intro",
    "accused me of 'using trauma for views' while using my face for views",
    "posted 'accountability culture is toxic' right after lying about me",
    "said I 'can't handle criticism' because I deleted spam slurs",
    "turned my supportive comment into a 'fan cringe' compilation",
    "claimed I 'harassed' them for asking them to stop tagging me in drama",
    "said I 'love being oppressed' because I asked for basic respect",
    "made a 'both sides' video where their side gets ten minutes and I get two seconds",
    "left 'L + ratio + you fell off' under a video about my first job interview",
    "said I 'look guilty' because I blinked during a serious sentence",
    "used my audio and added fake laughter over my sincere part",
    "posted 'I forgive you' publicly like I asked — I didn't",
    "said I 'weaponized my audience' because one person told them to stop lying",
    "made a 'red flag checklist' and every item was something I do when I'm anxious",
    "claimed I 'stole' a sound that's literally the app's free library",
    "said I 'beg for validation' for pinning a comment that said thanks",
    "replied 'skill issue' to me saying I was struggling with school",
    "made a 'I'm worried about you' post that was clearly for engagement",
    "said I 'fake positivity' because I said 'you got this' once",
    "accused me of 'ragebait' for telling a normal story with normal emotion",
    "posted my face next to 'main character syndrome' in a diagnosis meme",
    "said I 'can't read the room' in a room they stormed into",
    "used my clip as the 'bad example' in a tutorial about 'how not to film'",
    "left 'this is embarrassing' under a video I worked on for two weeks",
    "claimed I 'copied their aesthetic' because we both used daylight",
    "said I 'talk like a pick-me' for saying I don't like drama",
    "made a 'I don't remember saying that' moment after screenshots exist",
    "posted 'stop making everything about you' on a video literally about me",
    "said I 'love being the victim' because I said 'that hurt'",
    "turned my supportive DM into a 'creepy stan' joke on their story",
    "accused me of 'stalking' for viewing their public story they tagged me in",
    "said I 'need to log off' because I corrected a false claim once",
    "made a 'I'm the bigger person' video while zooming on my typos",
    "left 'rent free' like they're not the one posting me weekly",
    "said I 'can't take accountability' because I said I didn't do the thing",
    "used my face in a thumbnail titled 'the internet's worst takes'",
    "claimed I 'started a smear campaign' because I told the truth calmly",
    "said I 'fake cry' because my allergies were acting up on camera",
    "posted 'touch grass' from a bedroom that hasn't seen grass since 2019",
    "made a 'I'm shaking' video where they're not shaking, I'm just stressed",
    "said I 'weaponized trauma' for mentioning a boundary out loud",
    "left 'this ain't it chief' under a charity fundraiser clip I shared",
    "accused me of 'clout farming' a tragedy I never mentioned",
    "said I 'talk too fast' then sped my clip up to make me sound manic",
    "posted 'be real' then refused every real answer I gave",
    "claimed I 'copied their editing' because we both used jump cuts and music",
    "said I 'love chaos' because I asked them to stop stirring it under my posts",
    "made a 'I'm not reading all that' reply to a two-sentence clarification",
    "used my username in a 'toxic usernames' tier list next to slur-adjacent jokes",
    "said I 'need Jesus' because I disagreed with their take on cereal",
    "posted 'cope seethe mald' under a video where I said I was proud of my grade",
    "claimed I 'harassed their mods' for reporting spam bots",
    "said I 'fake woke' for asking people not to mock someone's accent",
    "made a 'I'm done with drama' post and tagged me eleven times",
    "left 'who is this' under a video with my name in the title",
    "said I 'look like I argue in comment sections' — they ARE the comment section",
    "turned my face into a 'this you?' meme under unrelated drama",
    "posted 'accountability' then deleted every comment that asked for receipts",
    "said I 'can't handle fame' because I have eight hundred followers",
    "used my clip in a 'worst advice on the internet' compilation — it wasn't advice",
    "claimed I 'threatened' them for saying 'please take the video down'",
    "said I 'love being canceled' because I logged off for a day",
    "made a 'I'm shaking and crying' thumbnail with dry eyes and a smirk",
    "left 'mid' under a video I made for my little sister's birthday",
    "accused me of 'stealing valor' for making a joke about studying too hard",
    "said I 'fake deep' because I quoted a song lyric in a caption",
    "posted 'skill issue' under me explaining I have slow Wi-Fi",
    "claimed I 'started a pile-on' because people disagreed with their lie",
    "said I 'need to humble myself' for being proud of something small",
    "used my voice note in a 'cringe compilation' with zero context",
    "made a 'I'm worried about your mental health' comment pinned for drama",
    "said I 'weaponized niceness' for apologizing even when I wasn't wrong",
    "posted 'ratio' under my college acceptance announcement",
    "claimed I 'copied their brand' because we both use lowercase titles sometimes",
    "said I 'talk like a villain' because I used the word 'boundary'",
    "left 'L' under a video about my first paycheck — literal paycheck",
    "turned my supportive comment into 'proof I'm obsessed' in a Notes app screenshot",
    "said I 'fake trauma' for mentioning I get anxious before presentations",
    "made a 'I'm not naming names' video with my hoodie and my lamp in frame",
    "posted 'cope harder' under me saying I was proud I didn't quit",
    "claimed I 'stalk their content' because the algorithm showed me their video",
    "said I 'love playing victim' because I said 'that wasn't true'",
    "used my face as the 'before' in a glow-up meme I never agreed to",
    "left 'seek grass' on a hiking video — they didn't watch past two seconds",
    "said I 'talk like a Reddit post' because I used the word 'respectfully'",
    "made a 'I'm shaking' storytime where the villain is clearly me, by description",
    "posted 'you're not innocent' because I liked a friend's comment",
    "claimed I 'doxxed' their city by saying 'we go to the same school event'",
    "said I 'fake being busy' because I didn't answer during class hours",
    "used my clip in a 'people who shouldn't have platforms' rant",
    "left 'this is giving desperate' under a video where I said I'm lonely",
    "said I 'weaponized the algorithm' because my video did better than theirs",
    "made a 'I'm not mad' tweet thread at 3 a.m. with my face as the header",
    "posted 'touch grass' then admitted they haven't been outside either",
    "claimed I 'harassed' them for asking their fans to stop spamming me",
    "said I 'love being oppressed' because I asked them to spell my name right",
    "turned my stutter into a 'comedy bit' with a laugh track",
    "left 'who asked + nobody cares' under a video literally addressed to nobody",
    "said I 'fake activism' for asking people to be kind in comments",
    "made a 'I'm the victim here' live while reading my private joke out loud",
    "posted 'cope' under me saying I'm proud I stood up for myself",
    "claimed I 'copied their trauma story' because we both mentioned exam stress",
    "said I 'talk too loud' then boosted my audio to clip and called it 'yelling'",
    "used my yearbook photo in a 'people who peaked in middle school' post",
    "left 'embarrassing' under a video I made to practice English",
    "said I 'weaponized friends' because two people said 'that's not what happened'",
    "made a 'I'm not reading that' screenshot of my two-line explanation",
    "posted 'ratio + L + ban' under a video about my cat",
    "claimed I 'started a hate campaign' because people downvoted their lie",
    "said I 'fake humble' for saying I'm still learning",
    "turned my face into a reaction meme titled 'instant regret'",
    "left 'you tried' under a video I spent thirty hours editing",
    "said I 'love attention' because I turned stitch notifications on",
    "made a 'I'm worried' DM then screenshotted my 'thanks' as 'weird'",
    "posted 'cope' under me venting about family stress — zero empathy",
    "claimed I 'copied their catchphrase' because we both said 'no cap' once",
    "said I 'look like I start drama' while starting drama under my pinned comment",
    "used my audio and pretended the emotional part was 'ironic' so people laughed at me",
    "left 'mid + ratio + fell off + cope' under my graduation recap",
    "said I 'weaponized therapy words' for saying 'I feel dismissed'",
    "made a 'I'm shaking' video about me that used my yearbook name in tags",
    "posted 'nobody asked' on a Q&A video that literally opened with prompts",
    "claimed I 'harassed' them for commenting 'this isn't true' once",
    "said I 'fake niceness for clout' because I fundraised five dollars transparently",
    "turned my supportive reply into 'proof I'm obsessed' with red circles and arrows",
    "left 'skill issue' under me explaining I have a learning disability accommodation",
    "said I 'love being the main character' for asking them to stop lying publicly",
    "made a 'I'm not obsessed' compilation that is ninety percent my face",
    "posted 'cope harder' under me saying I'm scared for my finals",
    "claimed I 'stole their editor' because we both hired the same public Fiverr gig",
    "said I 'talk like a lawsuit' for saying 'please don't misquote me'",
    "used my clip in a 'worst friends on the internet' list because I said no once",
    "left 'this is cringe' under a video my grandma liked",
    "said I 'weaponized the block button' for blocking someone who sent me slurs",
    "made a 'I'm done' post and then posted six more times about me that day",
)

_SPEEDRUN_RUNS: tuple[str, ...] = (
    "Any percent first period, glitches allowed",
    "No major cutscenes, homework skipped",
    "Pacifist percent — zero eye contact with the teacher",
    "New Game Plus, emotional damage carried over",
    "Randomizer seed: Monday",
)

_SPEEDRUN_SPLITS: tuple[tuple[str, str], ...] = (
    ("Alarm cancel", "frame-perfect snooze, we lose eighteen seconds but save sanity"),
    ("Bus load", "optimal seat — window, charger side, NPC ignored"),
    ("Hallway segment", "clip through crowd using the 'late kid' strats"),
    ("Lunch split", "trade macro: fries for fruit roll-up, positive RNG"),
    ("Last class", "boss phase: participation question — we tank it with confidence"),
    ("Bell PB", "personal best exit, no homework collected, any percent complete"),
)


def script_small_creator_wronged() -> str:
    """Storytime: a creator in the 50–500 sub band did something petty online (satire)."""
    subs = random.randint(50, 500)
    sub_txt = str(subs)
    niche = _pick(_SMALL_CREATOR_NICHES)
    wrong = _pick(_SMALL_CREATOR_WRONGS)
    closer = random.choice(
        [
            "If you've ever been personally victimized by a niche algorithm, you get it.",
            "Not naming names. The For You page already did that.",
            "Anyway, stay hydrated and lock your comments if you see them coming.",
            "I'm not mad. I'm taking notes for my villain arc that stays PG.",
        ]
    )
    return (
        f"Okay, storytime. A creator with {sub_txt} subscribers — we're talking that awkward zone "
        f"between fifty and five hundred, where they're not huge but they can still move like the main character — "
        f"anyway, picture this niche: {niche}. "
        f"They did something to me online. Specifically: {wrong}. "
        f"{closer}"
    )


def script_speedrun_school() -> str:
    """Gaming speedrun commentary over a relatable school day (12–17)."""
    category = _pick(_SPEEDRUN_RUNS)
    picks = list(_SPEEDRUN_SPLITS)
    random.shuffle(picks)
    chosen = picks[:4]
    splits_txt = " ".join(f"Split — {name}: {line}. " for name, line in chosen)
    return (
        f"Yo, speedrun commentary. Category: {category}. "
        f"Runner notes: if you're twelve to seventeen, this is your any percent. "
        f"{splits_txt}"
        "Time ends when the bell hits — thanks for watching, subscribe for patch notes."
    )
