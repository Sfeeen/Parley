"""The Parley wordlist: the vocabulary of every watchword and every fingerprint.

These words are spoken out loud -- read off a screen by one person and typed in by
another, often over a bad phone line.  The list is therefore curated, not scraped:

* exactly 2048 entries, so one word carries exactly 11 bits and a five-word
  watchword carries exactly 55 bits (SPEC 3.1);
* 3-7 ASCII lowercase letters, no apostrophes, no proper nouns;
* no two entries within Levenshtein distance 1, so a single misheard or mistyped
  letter can never turn one valid word into another;
* no entry is a substring of another, which kills the "did you say *art* or
  *part*?" class of confusion;
* no two members of a known homophone group (see ``HOMOPHONE_GROUPS``);
* no entry whose British and American spellings differ -- "harbour"/"harbor" is
  exactly the trap this list exists to avoid;
* concrete, common, inoffensive English.

``WORDS`` is **normative and frozen**.  Both ``generate_watchword`` and
``fingerprint`` index into it, so reordering it or changing its length would
change the fingerprint every participant reads aloud to verify they joined the
same parley.  Append-only is not safe either: 2048 is load-bearing.
"""
from __future__ import annotations

__all__ = ["WORDS", "WORD_COUNT", "BITS_PER_WORD", "HOMOPHONE_GROUPS",
           "assert_wordlist_sane"]

WORDS = (
    "abacus", "abbey", "above", "academy", "accept", "account", "acrobat", "acrylic", "action",
    "active", "adapter", "adjust", "adobe", "adult", "aerial", "agate", "agency", "agenda",
    "airline", "airport", "aisle", "alarm", "album", "alcohol", "alcove", "alder", "algebra",
    "align", "almanac", "along", "alpaca", "amber", "amount", "ampoule", "anatomy", "anchor",
    "anchovy", "andiron", "anemone", "animal", "ankle", "anneal", "anorak", "answer", "antenna",
    "anthem", "antique", "antler", "anvil", "apiary", "apology", "apparel", "apricot", "apron",
    "aquatic", "arch", "arena", "armada", "armrest", "aroma", "arrange", "arrival", "arrive",
    "artery", "article", "artisan", "artist", "artwork", "asleep", "aspen", "asphalt",
    "aspirin", "athlete", "atoll", "atomic", "atrium", "attach", "attic", "auger", "aurora",
    "author", "autumn", "avatar", "average", "aviator", "avocado", "awake", "award", "awning",
    "axiom", "azalea", "azure", "babble", "back", "bacon", "badger", "bagel", "baggage",
    "bagpipe", "bait", "baker", "baking", "balcony", "ballad", "ballast", "balloon", "ballot",
    "balmy", "bamboo", "banana", "band", "bangle", "banish", "banjo", "banner", "banquet",
    "barber", "bargain", "barista", "barley", "barrel", "barrier", "basalt", "basil", "basket",
    "bassoon", "bastion", "bathe", "bathtub", "batten", "battery", "bay", "bazaar", "beading",
    "bear", "beauty", "beaver", "bedroom", "bedtime", "bee", "begin", "behalf", "behind",
    "beige", "belfry", "bellows", "belong", "belt", "bench", "beneath", "benefit", "beret",
    "berry", "between", "beyond", "bike", "binary", "binder", "binding", "biology", "biplane",
    "birch", "biscuit", "bishop", "bison", "bisque", "bistro", "blame", "blanket", "bleach",
    "bleat", "blender", "blind", "blonde", "bloom", "blossom", "blouse", "blue", "blunt",
    "board", "bobbin", "bodice", "boiler", "boiling", "bolster", "bone", "bonfire", "bongo",
    "bonnet", "bonsai", "booklet", "bottle", "bottom", "bounce", "bouquet", "bowl", "boycott",
    "brace", "bracket", "brain", "bramble", "branch", "brandy", "bread", "breaker", "breathe",
    "breeze", "brewer", "brick", "brigade", "bright", "brisket", "brittle", "brocade", "brogue",
    "bronco", "bronze", "brooch", "brook", "broth", "brown", "browse", "bucket", "bud",
    "buffalo", "buffer", "bugle", "builder", "bull", "bumper", "bundle", "bunker", "buoy",
    "burden", "bureau", "burger", "burglar", "burlap", "burn", "burrito", "burrow", "bush",
    "butane", "butcher", "butter", "button", "buyer", "buzz", "bypass", "cabana", "cabaret",
    "cabbage", "cabinet", "cache", "cackle", "cactus", "caddy", "cadence", "cadet", "cake",
    "calcite", "calcium", "calf", "calico", "caliper", "camel", "camera", "camphor", "canary",
    "candid", "candle", "canine", "canoe", "canopy", "canteen", "canvas", "canyon", "capable",
    "capsize", "capstan", "capsule", "caption", "captive", "capture", "carafe", "caramel",
    "carcass", "cardiac", "career", "careful", "caress", "carol", "carpet", "carry", "carton",
    "carving", "cascade", "cashew", "cashier", "castle", "casual", "cat", "cavalry", "cavern",
    "caviar", "cavity", "cedar", "ceiling", "celery", "cellar", "cellist", "cello", "cement",
    "census", "central", "ceramic", "cereal", "chair", "chalice", "chalk", "chamois", "chance",
    "channel", "chapel", "charger", "chariot", "chase", "chassis", "chatter", "checker",
    "cheddar", "cheek", "cheese", "cheetah", "chef", "cherish", "cherry", "cherub", "chest",
    "chevron", "chewy", "chick", "chicory", "chiffon", "chili", "chimney", "chin", "chisel",
    "chive", "choice", "choose", "chop", "chorus", "chowder", "chrome", "chubby", "chuckle",
    "church", "churn", "chutney", "cider", "cigar", "cinema", "cipher", "circle", "circuit",
    "circus", "citadel", "citizen", "city", "civic", "clamp", "clang", "class", "clause",
    "clay", "cleaver", "clerk", "client", "cliff", "climate", "climax", "climb", "clinic",
    "clinker", "clique", "clock", "clog", "closet", "cloth", "cloud", "clover", "clumsy",
    "cluster", "clutch", "coarse", "coaster", "cobalt", "cobbler", "cobra", "cobweb", "cockpit",
    "cocoa", "cocoon", "coffee", "cohort", "collage", "collect", "collie", "cologne", "colt",
    "column", "combine", "comedy", "comet", "comfort", "comma", "comment", "common", "company",
    "compare", "compass", "compile", "compost", "comrade", "conceal", "concern", "condor",
    "conduit", "confess", "confirm", "conga", "conjure", "connect", "conquer", "consent",
    "console", "consume", "contact", "contain", "context", "contour", "control", "convert",
    "convex", "convoy", "cookie", "cooking", "cookout", "coolant", "cooler", "coping", "copper",
    "copse", "cordial", "corn", "corona", "coroner", "correct", "cortex", "cosmic", "cosmos",
    "cottage", "cotton", "couch", "cougar", "council", "counter", "county", "coupon", "courage",
    "courier", "court", "cousin", "cow", "coyote", "crab", "cracker", "cranium", "crate",
    "crawl", "cream", "creel", "crevice", "cricket", "crimson", "crisis", "crisp", "crochet",
    "croquet", "cross", "crowbar", "crowd", "crucial", "cruise", "crumb", "crumple", "crunchy",
    "crusade", "crust", "crystal", "cube", "cuckoo", "cuddle", "cuff", "cuisine", "culprit",
    "culvert", "cumin", "cup", "curator", "curious", "curlew", "curly", "current", "cursor",
    "curtain", "curve", "cushion", "custody", "custom", "cutlery", "cutlet", "cycle", "cycling",
    "cyclist", "cyclone", "cymbal", "cypress", "dahlia", "dainty", "daisy", "damage", "damask",
    "damsel", "dancer", "dark", "darning", "dash", "dazzle", "deacon", "dealer", "debris",
    "decant", "decibel", "decide", "decimal", "declare", "decline", "decoy", "deer", "default",
    "defend", "deficit", "degrade", "degree", "delete", "deliver", "delta", "demand", "denial",
    "denim", "dense", "density", "dented", "dentist", "depart", "depict", "deposit", "depth",
    "derby", "derrick", "descend", "desert", "deserve", "design", "desire", "desk", "despair",
    "detach", "detain", "detour", "develop", "deviate", "devote", "dialect", "diamond",
    "diaper", "diary", "diesel", "differ", "digest", "digger", "digit", "dilemma", "dimness",
    "dimpled", "diner", "dinghy", "diocese", "diode", "dioxide", "diploma", "direct", "disable",
    "discard", "disco", "discus", "disgust", "dislike", "dismal", "dismiss", "disown",
    "display", "dispute", "disrupt", "distaff", "distant", "disturb", "ditto", "dive", "divide",
    "divorce", "divulge", "docile", "doctor", "dog", "doily", "doll", "dolphin", "domino",
    "donate", "door", "dormant", "dormer", "dorsal", "dosage", "double", "dowel", "drag",
    "drama", "drastic", "drawer", "drawing", "dreamer", "dreary", "dress", "drift", "drinker",
    "drizzle", "drone", "drop", "drover", "drowsy", "druid", "drum", "dryer", "duchess", "duck",
    "dugout", "dune", "duplex", "dust", "duvet", "dynamic", "dynamo", "dynasty", "eagle",
    "earlobe", "early", "earmark", "earnest", "earwig", "eaves", "echelon", "eclipse",
    "ecology", "economy", "edible", "edifice", "editor", "effigy", "egg", "elbow", "emblem",
    "emerald", "empty", "engine", "enigma", "enter", "equinox", "eraser", "estuary", "evening",
    "examine", "exhale", "expense", "expert", "explain", "explore", "extrude", "eyelet", "face",
    "factory", "faded", "falcon", "family", "farmer", "fasten", "father", "fathom", "fearful",
    "feast", "feeble", "feeder", "fence", "fencing", "fern", "ferret", "fiddle", "field",
    "filing", "fill", "filter", "final", "finch", "find", "finish", "firefly", "fish", "fjord",
    "flake", "flannel", "flat", "fleck", "fleece", "flicker", "flint", "floor", "florist",
    "flounce", "flower", "flurry", "flute", "flux", "foal", "foggy", "folder", "folding",
    "follow", "forager", "forbid", "forceps", "forest", "forge", "fork", "formula", "forward",
    "fossil", "foundry", "fox", "foyer", "fragile", "frame", "frayed", "fresh", "fridge",
    "friend", "frieze", "fritter", "frog", "frost", "frying", "fugue", "fuller", "fur", "fuse",
    "futon", "fuzzy", "gadget", "galaxy", "gale", "gallery", "gallop", "galosh", "garage",
    "garden", "garland", "garlic", "garnet", "gateway", "gazebo", "gazelle", "gecko", "gelding",
    "gentle", "gerbil", "giant", "gibbon", "giggle", "ginger", "gingham", "girder", "girl",
    "gizmo", "glacier", "glance", "glaze", "gleam", "glimmer", "glimpse", "gloomy", "glossy",
    "glove", "glut", "gnarled", "gnaw", "goat", "goblet", "gold", "gondola", "goose", "gouge",
    "graft", "gram", "granary", "granite", "granule", "grape", "grass", "grater", "gravel",
    "gravy", "greasy", "green", "griddle", "grief", "grill", "grind", "grip", "gritty",
    "grocer", "grommet", "groom", "groove", "group", "grow", "guard", "guide", "guitar", "gulf",
    "gully", "gumbo", "gymnast", "gypsum", "habit", "haggle", "hail", "halibut", "hallway",
    "hamlet", "hammer", "hammock", "hamster", "handful", "hangar", "happy", "hare", "harmful",
    "harmony", "harness", "harvest", "hawk", "hazel", "head", "heart", "heavy", "hedge",
    "heifer", "helmet", "help", "hen", "herb", "herder", "heron", "herring", "hessian",
    "hexagon", "hiking", "hinge", "hippo", "hiss", "hockey", "home", "honesty", "honey",
    "hoodie", "hoof", "hopeful", "horizon", "hornet", "horse", "hour", "hover", "huge",
    "humble", "humid", "humus", "hunter", "hurdle", "hurry", "hutch", "hymn", "ibex", "iceberg",
    "ignore", "iguana", "imagine", "impala", "improve", "index", "indigo", "infant", "ingot",
    "inhale", "initial", "inkwell", "inlet", "inside", "inspect", "intend", "invent", "invoice",
    "inward", "iris", "ironing", "island", "isthmus", "ivory", "ivy", "jackdaw", "jacket",
    "jade", "jagged", "jaguar", "jam", "jasmine", "jeep", "jetty", "jigsaw", "joiner", "joist",
    "jolly", "journal", "journey", "joy", "judo", "juice", "jumping", "jungle", "juniper",
    "jury", "jute", "karate", "kayak", "keeper", "kennel", "kestrel", "ketchup", "kettle",
    "keyword", "khaki", "kilo", "kiosk", "kite", "kitten", "kitty", "knead", "knee", "knock",
    "knotty", "koala", "lacquer", "ladder", "ladle", "lagoon", "lair", "lamb", "lampoon",
    "landing", "lanolin", "lantern", "lapel", "laptop", "large", "lariat", "lasagna", "latency",
    "lateral", "laugh", "laundry", "laurel", "lava", "lavish", "lawful", "lawn", "lawsuit",
    "layer", "layout", "leaf", "league", "leakage", "learn", "leash", "leather", "lecture",
    "leech", "leek", "leeward", "leeway", "legacy", "legend", "legible", "legwork", "leisure",
    "lemming", "lemon", "lemur", "length", "lenient", "lens", "lentil", "leopard", "lesson",
    "letter", "lettuce", "level", "levity", "lexicon", "liable", "liberal", "liberty",
    "library", "lid", "lift", "light", "lilac", "lily", "lime", "limpet", "linear", "linen",
    "lineup", "linnet", "lintel", "lion", "liquid", "listen", "litany", "lithium", "litmus",
    "little", "liturgy", "lively", "lizard", "llama", "loafer", "lobby", "lobster", "local",
    "locker", "lockout", "locust", "lodge", "logbook", "logic", "logout", "loiter", "lonely",
    "loofah", "look", "loosen", "lotus", "lounge", "lovable", "lowland", "loyalty", "lozenge",
    "lucky", "luggage", "lullaby", "lumber", "lumpy", "lunar", "lupin", "lurch", "luxury",
    "lyceum", "lyric", "macaw", "machete", "macro", "maestro", "magenta", "magic", "magma",
    "magnet", "magnify", "magpie", "mahjong", "mailbox", "mailman", "majesty", "makeup",
    "malady", "mallard", "mammal", "manage", "mane", "manful", "mango", "manhole", "mania",
    "manikin", "mansion", "mantel", "mantra", "manure", "maple", "maraca", "marble", "margin",
    "marimba", "mariner", "marital", "market", "marmot", "maroon", "marquee", "marsh", "marten",
    "marvel", "mascara", "masher", "mason", "massive", "mast", "matador", "matches", "matrix",
    "matte", "mattock", "mauve", "maximum", "mayfly", "mayor", "maypole", "meadow", "meander",
    "meaning", "measure", "medal", "median", "mediate", "medical", "medium", "medley",
    "megaton", "melange", "melodic", "melody", "melon", "member", "memento", "memoir", "mend",
    "menthol", "mention", "mercury", "merger", "merit", "merlin", "mermaid", "mesa", "message",
    "messy", "meteor", "methane", "method", "metric", "metro", "mica", "microbe", "micron",
    "midair", "midday", "midland", "midriff", "midterm", "midweek", "midwife", "mighty",
    "migrant", "mileage", "milieu", "milk", "millet", "mimic", "mimosa", "minaret", "mindful",
    "mineral", "mingle", "minibus", "minion", "minnow", "minster", "mint", "minute", "miracle",
    "mirage", "mire", "mirror", "miser", "misfit", "mishap", "mislaid", "mistake", "misty",
    "mobile", "mocha", "model", "modern", "modest", "modular", "module", "mogul", "mohair",
    "moisten", "mollusc", "molten", "moment", "monger", "mongrel", "moniker", "monitor",
    "monkey", "monocle", "monsoon", "montage", "monthly", "moon", "mooring", "mop", "moraine",
    "moray", "morsel", "mortar", "mortise", "mosaic", "mosque", "moss", "moth", "motif",
    "motley", "motor", "mottled", "mouse", "movie", "muddy", "muffin", "mug", "mulch", "mule",
    "mundane", "mural", "murmur", "muscle", "museum", "music", "muskrat", "muslin", "mustang",
    "muster", "mutant", "muted", "mutual", "myopia", "myriad", "myrtle", "mystery", "mystic",
    "mystify", "nadir", "name", "napkin", "narrate", "narrow", "narwhal", "nasal", "nascent",
    "natural", "nature", "navy", "nearby", "nebula", "neck", "nectar", "needle", "needy",
    "neglect", "neigh", "nephew", "nerve", "nervous", "nestle", "network", "neural", "neuron",
    "newborn", "newel", "newline", "newness", "newt", "nexus", "nickel", "niece", "nightly",
    "nimble", "ninety", "ninth", "nodal", "noggin", "noisy", "nomad", "nominal", "nominee",
    "nonstop", "noodle", "north", "nosing", "nostril", "notably", "notary", "note", "notify",
    "nougat", "noun", "nourish", "novella", "novelty", "novice", "nowhere", "nozzle", "nuance",
    "nuclear", "nucleus", "nudge", "nugget", "nullify", "numeral", "numeric", "nunnery",
    "nuptial", "nurse", "nurture", "nutmeg", "nylon", "nymph", "oak", "oarlock", "oarsman",
    "oasis", "oatmeal", "observe", "ocean", "octagon", "octave", "olive", "onion", "operate",
    "option", "orange", "orbit", "orchard", "orchid", "order", "oregano", "organ", "origami",
    "osprey", "ostrich", "otter", "ottoman", "outline", "outside", "outward", "oven", "overall",
    "oyster", "package", "paddle", "paddock", "page", "painter", "pallet", "palm", "panda",
    "panel", "panic", "pantry", "pants", "papaya", "paper", "paprika", "papyrus", "parade",
    "parapet", "parcel", "parent", "parfait", "parish", "parka", "parrot", "parsley", "parsnip",
    "partner", "pasta", "pasture", "path", "patient", "patio", "patrol", "patty", "pause",
    "paving", "paw", "payment", "peach", "peacock", "peanut", "pebble", "peel", "pelican",
    "pencil", "penguin", "pennant", "pepper", "perfume", "permit", "petal", "pewter", "phial",
    "photo", "phrase", "piano", "pickle", "picnic", "pig", "pilaf", "pilgrim", "pillar",
    "pilot", "pine", "pistil", "piston", "pita", "pitch", "pitted", "pixel", "pizza", "plaice",
    "planet", "plank", "plastic", "plate", "platter", "playful", "plaza", "pliers", "plinth",
    "plumb", "pocket", "pod", "poker", "polish", "polite", "pollen", "pollock", "pompom",
    "poncho", "pony", "porch", "porous", "porter", "portion", "potato", "powdery", "prairie",
    "prefer", "prelude", "prepare", "present", "pretzel", "prickly", "pride", "primer", "print",
    "priory", "prism", "probe", "problem", "produce", "project", "promise", "prune", "pudding",
    "pulley", "pulp", "pulsar", "pumice", "pumpkin", "punch", "punt", "puppet", "puppy",
    "purple", "purr", "puzzle", "pyramid", "quack", "quail", "quartet", "quartz", "quasar",
    "quay", "queen", "quench", "quiet", "quintet", "rabbit", "radar", "radio", "radish",
    "railing", "rainbow", "raisin", "rampart", "rapid", "ratchet", "rattle", "raven", "ravine",
    "ravioli", "razor", "reap", "rebate", "rebound", "recall", "receipt", "recipe", "record",
    "red", "referee", "refrain", "refund", "regatta", "region", "regret", "relay", "release",
    "relief", "relish", "remain", "remedy", "render", "repair", "repeat", "report", "request",
    "restful", "restore", "result", "resume", "retail", "return", "reunion", "revolve", "rhino",
    "rhizome", "rhythm", "rib", "rice", "rigging", "rigid", "rinse", "ripple", "ritual",
    "river", "roach", "roar", "robin", "rock", "roller", "rolling", "roofer", "rooftop", "root",
    "rose", "rotate", "rough", "round", "router", "rowing", "rubber", "ruby", "rudder",
    "ruffle", "ruler", "runner", "running", "runway", "russet", "rustle", "sachet", "sadness",
    "saffron", "sailor", "salad", "salmon", "salty", "sandal", "sandbar", "sander", "sandy",
    "sap", "sardine", "satchel", "sauce", "sausage", "savoury", "scale", "scamper", "scanner",
    "scarf", "scarlet", "school", "scone", "scooter", "screech", "screen", "screw", "seal",
    "season", "second", "secret", "seed", "seesaw", "segment", "select", "sepia", "sequin",
    "server", "serving", "sextant", "shadow", "shaft", "shallow", "shame", "shampoo", "shark",
    "shaver", "shaving", "shawl", "sheep", "shelf", "shelter", "shimmer", "shingle", "shiny",
    "ship", "shirt", "shout", "shovel", "shower", "shrimp", "shrine", "shroud", "shrub",
    "shudder", "shuttle", "sidecar", "sieve", "sigh", "signal", "silent", "silky", "silver",
    "sink", "sisal", "sister", "sizzle", "skate", "skein", "sketch", "skid", "skiing",
    "skillet", "skunk", "sky", "sledge", "sleeper", "sleet", "sleeve", "slimy", "slipper",
    "slope", "slug", "sluice", "slush", "small", "smile", "smoker", "smooth", "snail", "snake",
    "sneaker", "sniff", "snow", "snuffer", "soccer", "soda", "soft", "soil", "solar", "soloist",
    "sonata", "song", "sorbet", "sorrow", "soup", "spade", "spaniel", "sparkle", "sparrow",
    "spatula", "speak", "sphere", "spice", "spider", "spinach", "spindle", "spinner", "spiral",
    "splash", "sponge", "spoon", "spore", "sprat", "sprayer", "spread", "spring", "sprout",
    "spruce", "squall", "square", "squash", "squeak", "squeeze", "squid", "stadium", "stalk",
    "stamen", "staple", "star", "station", "statue", "steady", "steel", "stencil", "steppe",
    "stern", "stew", "sticker", "sticky", "stigma", "stony", "stool", "storage", "stork",
    "stormy", "stove", "strain", "strap", "stream", "stretch", "stride", "stroke", "stroll",
    "strudel", "stubble", "studded", "student", "studio", "stump", "sturdy", "subject",
    "subway", "suds", "sugar", "suggest", "summary", "summer", "summit", "sundae", "sundial",
    "sundown", "sunny", "sunrise", "sunset", "sunup", "super", "suppose", "surfing", "surgery",
    "swaddle", "swamp", "swan", "sweater", "swift", "swim", "swing", "switch", "swivel",
    "swoop", "syrup", "system", "table", "tactful", "taffeta", "takeoff", "tally", "tandem",
    "tangy", "tankard", "tanker", "tape", "tapir", "tartan", "tassel", "taxi", "teacher",
    "team", "teddy", "temple", "tempo", "tendril", "tennis", "tenon", "tent", "terrace",
    "terrier", "thermos", "thimble", "think", "thorn", "thread", "throw", "thrush", "thud",
    "thumb", "thunder", "ticket", "tidy", "tiger", "tighten", "tights", "tile", "tiller",
    "timpani", "tin", "toad", "toast", "today", "toddler", "toggle", "token", "tomato", "tongs",
    "tongue", "toolbar", "toolbox", "toolkit", "tooth", "topaz", "topic", "topping", "topple",
    "torrent", "toucan", "tower", "town", "track", "tractor", "traffic", "trail", "transom",
    "trawler", "tray", "treaty", "tree", "trellis", "tremble", "trifle", "trim", "trinket",
    "trivet", "trolley", "trophy", "troupe", "trouser", "trout", "trowel", "trudge", "trumpet",
    "trunk", "truss", "tuba", "tulip", "tumbler", "tundra", "tunic", "tunnel", "turban",
    "turbine", "turkey", "turnip", "turret", "turtle", "tusk", "tweed", "twig", "twill",
    "twine", "twisted", "typhoon", "ukulele", "umpire", "uncle", "uniform", "unload", "unlock",
    "unpack", "untie", "unwrap", "upward", "useful", "useless", "utility", "valance", "valley",
    "valve", "van", "varnish", "vector", "veil", "velcro", "vellum", "velvet", "vendor",
    "veranda", "verse", "veteran", "view", "village", "vintner", "viola", "violet", "violin",
    "visa", "visitor", "vitamin", "vivid", "vole", "volume", "voucher", "voyage", "vulture",
    "waffle", "wagon", "walking", "wall", "walnut", "walrus", "warped", "warren", "washing",
    "wasp", "watch", "water", "wavelet", "wax", "weasel", "weaving", "wedding", "weft",
    "weight", "weir", "welder", "wetland", "whale", "wharf", "wheel", "whirl", "whisk",
    "whisper", "whistle", "white", "wick", "wide", "widget", "width", "willow", "window",
    "windy", "winter", "wobbly", "wolf", "woman", "wonder", "wood", "woolly", "worm", "worry",
    "wreath", "wrench", "wrinkle", "wrist", "yacht", "yard", "yellow", "yew", "young", "zebra",
    "zenith", "zest", "zinc", "zipper",
)

WORD_COUNT = len(WORDS)
BITS_PER_WORD = 11          # 2 ** 11 == 2048; asserted below, not assumed

#: Homophone / near-homophone groups that touch this list.  At most one member of
#: any group may appear in ``WORDS`` -- otherwise two different watchwords would
#: sound identical over the phone.  Groups whose members are all absent are not
#: listed; these are the ones a future edit could actually violate.
HOMOPHONE_GROUPS = (
    ("band", "banned"),
    ("bare", "bear"),
    ("bazaar", "bizarre"),
    ("be", "bee"),
    ("berry", "bury"),
    ("blew", "blue"),
    ("board", "bored"),
    ("boy", "buoy"),
    ("bread", "bred"),
    ("broach", "brooch"),
    ("cache", "cash"),
    ("canvas", "canvass"),
    ("caught", "court"),
    ("cede", "seed"),
    ("ceiling", "sealing"),
    ("cellar", "seller"),
    ("cereal", "serial"),
    ("chile", "chili", "chilly"),
    ("chopper", "copper"),
    ("clause", "claws"),
    ("coarse", "course"),
    ("council", "counsel"),
    ("crews", "cruise"),
    ("currant", "current"),
    ("cymbal", "symbol"),
    ("dear", "deer"),
    ("dense", "dents"),
    ("desert", "dessert"),
    ("done", "dun", "dune"),
    ("ewe", "yew", "you"),
    ("eyelet", "islet"),
    ("find", "fine", "fined"),
    ("fir", "fur"),
    ("flour", "flower"),
    ("foreword", "forward"),
    ("frees", "freeze", "frieze"),
    ("grill", "grille"),
    ("hail", "hale"),
    ("hair", "hare"),
    ("hangar", "hanger"),
    ("hoarse", "horse"),
    ("hour", "our"),
    ("jam", "jamb"),
    ("knead", "kneed", "need"),
    ("knock", "nock"),
    ("lam", "lamb"),
    ("leach", "leech"),
    ("leaf", "lief"),
    ("leak", "leek"),
    ("lessen", "lesson"),
    ("lumbar", "lumber"),
    ("main", "mane"),
    ("mantel", "mantle"),
    ("mare", "mayor"),
    ("marquee", "marquis"),
    ("massed", "mast"),
    ("medal", "meddle", "metal", "mettle"),
    ("muscle", "mussel"),
    ("nay", "neigh"),
    ("palate", "palette", "pallet"),
    ("parish", "perish"),
    ("pause", "paws"),
    ("paw", "poor", "pore", "pour"),
    ("peal", "peel"),
    ("pedal", "peddle", "petal"),
    ("pencil", "pensile"),
    ("place", "plaice"),
    ("plum", "plumb"),
    ("pride", "pried"),
    ("quarts", "quartz"),
    ("read", "red", "reed"),
    ("roes", "rose", "rouse", "rows"),
    ("root", "route"),
    ("rough", "ruff"),
    ("steal", "steel", "stele"),
    ("step", "steppe"),
    ("summary", "summery"),
    ("team", "teem"),
    ("toad", "towed"),
    ("troop", "troupe"),
    ("tuba", "tuber"),
    ("vail", "vale", "veil"),
    ("wail", "whale"),
    ("wait", "weight"),
    ("wax", "whacks"),
    ("we're", "weir"),
    ("weal", "wheel"),
    ("whirl", "whorl"),
    ("white", "wight"),
    ("wood", "would"),
)


def _levenshtein_le1_key_set(word):
    """Every string reachable from ``word`` by deleting at most one character.

    Two words are within Levenshtein distance 1 if and only if these sets
    intersect, which turns an O(n^2) pairwise check into an O(n * len) one.
    """
    keys = {word}
    for i in range(len(word)):
        keys.add(word[:i] + word[i + 1:])
    return keys


def assert_wordlist_sane() -> None:
    """Re-prove every property the list claims, so a careless edit cannot ship.

    Called by the test-suite.  It is deliberately cheap enough (well under a
    second) to also be called from ``parley doctor``.
    """
    if len(WORDS) < 2048:
        raise AssertionError("wordlist has %d entries, need at least 2048" % len(WORDS))
    if 2 ** BITS_PER_WORD != len(WORDS):
        raise AssertionError(
            "wordlist length %d is not 2 ** BITS_PER_WORD (%d); the entropy claim in "
            "SPEC 3.1 and the fingerprint mapping both depend on it"
            % (len(WORDS), 2 ** BITS_PER_WORD))

    seen = set()
    for word in WORDS:
        if not isinstance(word, str):
            raise AssertionError("wordlist entry is not a string: %r" % (word,))
        if word in seen:
            raise AssertionError("duplicate wordlist entry: %s" % word)
        seen.add(word)
        if not 3 <= len(word) <= 7:
            raise AssertionError("%s: length %d outside 3..7" % (word, len(word)))
        for ch in word:
            if not ("a" <= ch <= "z"):
                raise AssertionError("%s: not ASCII lowercase a-z" % word)

    if list(WORDS) != sorted(WORDS):
        raise AssertionError("wordlist is not sorted; order is normative, keep it stable")

    claimed = {}
    for word in WORDS:
        for key in _levenshtein_le1_key_set(word):
            other = claimed.get(key)
            if other is not None:
                raise AssertionError(
                    "%s and %s are within Levenshtein distance 1" % (other, word))
            claimed[key] = word

    for word in WORDS:
        for size in range(3, len(word)):
            for start in range(0, len(word) - size + 1):
                part = word[start:start + size]
                if part in seen:
                    raise AssertionError("%s contains the wordlist entry %s" % (word, part))

    for group in HOMOPHONE_GROUPS:
        present = [w for w in group if w in seen]
        if len(present) > 1:
            raise AssertionError(
                "homophones both present in the wordlist: %s" % ", ".join(present))


if __name__ == "__main__":        # pragma: no cover - convenience for maintainers
    assert_wordlist_sane()
    print("%d words, %d bits each, %d bits per 5-word watchword"
          % (len(WORDS), BITS_PER_WORD, 5 * BITS_PER_WORD))
