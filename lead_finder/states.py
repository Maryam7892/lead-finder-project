"""Static reference data: US states + a few common service presets."""

US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
}

SERVICE_PRESETS = [
    "Bookkeeping",
    # "Accounting",
    # "Tax Preparation",
    # "Payroll Services",
    # "CFO Advisory",
    # "Business Consulting",
    # "HR Outsourcing",
    # "Dental Billing",
]

# What actually shows in the sidebar dropdown right now. The rest of
# SERVICE_PRESETS (and their templates/OSM tags) stay fully built and ready —
# just deliberately not offered as a choice yet, per a decision to launch
# with one service at a time rather than all eight at once.
ACTIVE_SERVICES = [
    "Bookkeeping",
]

# For most services, we search for other companies offering that exact
# service (e.g. "Bookkeeping companies in Texas"). Dental Billing is
# different: the prospects are dental practices that *need* billing help,
# not other dental-billing companies. This maps a service to the kind of
# business its search queries should actually target; services not listed
# here just search for themselves, as before.
SERVICE_SEARCH_TARGETS = {
    "Dental Billing": "dental practices",
}


def get_search_target(service: str) -> str:
    return SERVICE_SEARCH_TARGETS.get(service, service)


# Bookkeeping doesn't target one kind of prospect — it targets several
# distinct verticals, each of which plausibly needs bookkeeping/accounting
# help for a different reason (accounting firms as a white-label/partner
# angle, everyone else because they don't want to run their own books).
# Each entry:
#   label        - shown in the UI / stored per-lead so you know which
#                  vertical a lead came from
#   query_terms  - list of noun-phrase synonyms used in web search queries.
#                  Each term gets its own full set of state+city query
#                  variants (see search.build_queries) — this is what covers
#                  how people actually describe the trade (a florist search
#                  that only ever says "florist" misses "flower shop" /
#                  "flower delivery" results), not just one fixed phrase.
#                  More terms = more coverage but proportionally more
#                  queries; the "Use extra search phrasings" toggle in the
#                  UI falls back to just query_terms[0] when turned off.
#   osm_tags     - OpenStreetMap (key, value) tags to query for this
#                  vertical; empty list means OSM has no good tag for it and
#                  this vertical relies on web search only. Queried as
#                  separate Overpass calls per tag (see osm_source.py) so
#                  one tag timing out on a big state doesn't lose the others.
#   keywords     - words that must appear somewhere in a result's title/
#                  snippet for it to count as relevant to this vertical
#                  (replaces the service-name-based relevance check when a
#                  service has verticals defined)
BOOKKEEPING_TARGET_VERTICALS = [
    {
        "label": "Accounting & Bookkeeping Firms",
        "query_terms": [
            "accounting and bookkeeping firms", "CPA firms",
            "tax and accounting services", "small business accountants",
        ],
        "osm_tags": [("office", "accountant"), ("office", "tax_advisor")],
        "keywords": ["accounting", "bookkeeping", "accountant", "cpa"],
    },
    {
        "label": "Dental Practices",
        "query_terms": [
            "dental practices", "dentists", "dental offices",
            "family dentistry practices",
        ],
        "osm_tags": [("amenity", "dentist")],
        "keywords": ["dental", "dentist", "dentistry", "orthodont"],
    },
    {
        "label": "Event Management",
        "query_terms": [
            "event management and wedding planning companies",
            "wedding planners", "event planners", "party planning companies",
        ],
        # OSM has one real tag for this trade (office=event_management) and
        # it's sparsely used — event/wedding planning isn't well-modeled in
        # OSM's schema the way a physical shop or office is, so this
        # vertical leans on web search more than most. Not a bug to fix by
        # inventing a tag that doesn't reflect real-world OSM usage.
        "osm_tags": [("office", "event_management")],
        "keywords": ["event", "events", "wedding", "planner", "planning"],
    },
    {
        "label": "Floral Businesses",
        "query_terms": [
            "florists and floral design businesses", "flower shops",
            "flower delivery services", "wedding florists", "event florists",
        ],
        # shop=florist is the primary tag; craft=florist covers florists
        # mapped as a workshop/craft rather than a shop, and
        # shop=garden_centre catches garden centers that also do floral
        # design/delivery — a plausible bookkeeping prospect even when
        # "florist" isn't their primary listed trade.
        "osm_tags": [("shop", "florist"), ("craft", "florist"), ("shop", "garden_centre")],
        "keywords": ["florist", "floral", "flower", "flowers", "bouquet"],
    },
    {
        "label": "Construction & Real Estate",
        "query_terms": [
            "construction and real estate companies", "general contractors",
            "real estate agencies", "home builders",
        ],
        "osm_tags": [("office", "estate_agent"), ("office", "construction_company")],
        "keywords": ["construction", "real estate", "realty", "builder", "contractor", "realtor"],
    },
    {
        "label": "Rental Properties (STR/LTR)",
        "query_terms": [
            "short term and long term rental property management companies",
            "property management companies", "vacation rental management companies",
            "airbnb property managers",
        ],
        "osm_tags": [("office", "property_management")],
        "keywords": ["rental", "rentals", "property management", "airbnb", "short-term", "long-term", "vacation rental"],
    },
    {
        "label": "Manufacturing",
        "query_terms": [
            "manufacturing companies", "manufacturers",
            "industrial manufacturers", "product manufacturing companies",
        ],
        # man_made=works is OSM's real tag for a factory/production plant —
        # narrow coverage (manufacturers are inconsistently mapped at all),
        # but better than the empty list this vertical had before.
        "osm_tags": [("man_made", "works")],
        "keywords": ["manufacturing", "manufacturer", "factory", "production", "industrial"],
    },
    {
        "label": "IT & Software",
        "query_terms": [
            "IT services and software companies", "IT support companies",
            "software development companies", "managed IT service providers",
        ],
        "osm_tags": [("office", "it"), ("office", "software")],
        "keywords": ["software", "it services", "technology", "tech company", "saas", "it support"],
    },
]

SERVICE_TARGET_VERTICALS = {
    "Bookkeeping": BOOKKEEPING_TARGET_VERTICALS,
}


def get_target_verticals(service: str):
    """Returns a list of vertical dicts for services with multi-vertical targeting, else None."""
    return SERVICE_TARGET_VERTICALS.get(service)

MAJOR_CITIES_BY_STATE = {
    "California": [
    "Los Angeles", "San Diego", "San Jose", "San Francisco", "Fresno", "Sacramento", "Long Beach", "Oakland", "Bakersfield", "Anaheim", "Riverside", "Irvine", "Santa Ana", "Chula Vista", "Stockton", "Fremont", "San Bernardino", "Modesto", "Fontana", "Oxnard", "Huntington Beach", "Glendale", "Elk Grove", "Ontario", "Santa Clarita", "Oceanside", "Rancho Cucamonga", "Santa Rosa", "Garden Grove", "Corona", "Lancaster", "Palmdale", "Salinas", "Hayward", "Pomona", "Sunnyvale", "Escondido", "Torrance", "Pasadena", "Orange", "Fullerton", "Roseville",
    "Adelanto", "Agoura Hills", "Alameda", "Albany", "Alhambra", "Aliso Viejo", "Alturas", "Amador City", "American Canyon", "Anaheim", "Anderson", "Angels Camp", "Antioch", "Apple Valley", "Arcadia", "Arcata", "Arroyo Grande", "Artesia", "Arvin", "Atascadero", "Atherton", "Atwater", "Auburn", "Avalon", "Avenal", "Azusa",
    "Baldwin Park", "Banning", "Barstow", "Beaumont", "Bell", "Bell Gardens", "Bellflower", "Belmont", "Belvedere", "Benicia", "Berkeley", "Beverly Hills", "Big Bear Lake", "Biggs", "Bishop", "Blue Lake", "Blythe", "Bradbury", "Brawley", "Brea", "Brentwood", "Brisbane", "Buellton", "Buena Park", "Burbank", "Burlingame",
    "Calabasas", "Calexico", "California City", "Calimesa", "Calistoga", "Camarillo", "Campbell", "Canyon Lake", "Capitola", "Carlsbad", "Carmel-by-the-Sea", "Carpinteria", "Carson", "Cathedral City", "Ceres", "Cerritos", "Chico", "Chino", "Chino Hills", "Chowchilla", "Citrus Heights", "Claremont", "Clayton", "Clearlake", "Cloverdale", "Clovis", "Coachella", "Coalinga", "Colfax", "Colma", "Colton", "Colusa", "Commerce", "Compton", "Concord", "Corcoran", "Corning", "Coronado", "Corte Madera", "Costa Mesa", "Cotati", "Covina", "Crescent City", "Cudahy", "Culver City", "Cupertino", "Cypress",
    "Daly City", "Dana Point", "Danville", "Davis", "Del Mar", "Del Rey Oaks", "Delano", "Desert Hot Springs", "Diamond Bar", "Dinuba", "Dixon", "Dorris", "Dos Palos", "Downey", "Duarte", "Dublin", "Dunsmuir",
    "East Palo Alto", "Eastvale", "El Cajon", "El Centro", "El Cerrito", "El Monte", "El Segundo", "Emeryville", "Encinitas", "Escalon", "Etna", "Eureka", "Exeter",
    "Fairfax", "Fairfield", "Farmersville", "Ferndale", "Fillmore", "Firebaugh", "Folsom", "Fort Bragg", "Fort Jones", "Fortuna", "Foster City", "Fountain Valley", "Fowler",
    "Galt", "Gardena", "Gasquet", "Gilroy", "Glendora", "Goleta", "Gonzales", "Grand Terrace", "Grass Valley", "Greenfield", "Gridley", "Grover Beach", "Guadalupe", "Gustine",
    "Half Moon Bay", "Hanford", "Hawaiian Gardens", "Hawthorne", "Healdsburg", "Hemet", "Hercules", "Hermosa Beach", "Hesperia", "Hidden Hills", "Highland", "Hillsborough", "Hollister", "Holtville", "Hughson", "Huntington Park", "Huron",
    "Imperial", "Imperial Beach", "Indian Wells", "Indio", "Industry", "Inglewood", "Ione", "Irwindale", "Isleton",
    "Jackson", "Jurupa Valley",
    "Kerman", "King City", "Kingsburg",
    "La Cañada Flintridge", "La Habra", "La Habra Heights", "La Mesa", "La Mirada", "La Palma", "La Puente", "La Quinta", "La Verne", "Lafayette", "Laguna Beach", "Laguna Hills", "Laguna Niguel", "Laguna Woods", "Lake Elsinore", "Lake Port", "Lake Forest", "Larkspur", "Lathrop", "Lawndale", "Lemon Grove", "Lemoore", "Lincoln", "Lindsay", "Live Oak", "Livermore", "Livingston", "Lodi", "Loma Linda", "Lomita", "Lompoc", "Loomis", "Los Alamitos", "Los Altos", "Los Altos Hills", "Los Banos", "Los Gatos", "Loyalton", "Lynwood",
    "Madera", "Malibu", "Mammoth Lakes", "Manhattan Beach", "Manteca", "Maricopa", "Marina", "Martinez", "Marysville", "Maywood", "McFarland", "Mendota", "Menifee", "Menlo Park", "Merced", "Mill Valley", "Millbrae", "Milpitas", "Mission Viejo", "Monrovia", "Montague", "Montclair", "Monte Sereno", "Montebello", "Monterey", "Monterey Park", "Moorpark", "Moraga", "Moreno Valley", "Morgan Hill", "Morro Bay", "Mountain House", "Mountain View", "Mt. Shasta", "Murrieta",
    "Napa", "National City", "Needles", "Nevada City", "Newark", "Newman", "Newport Beach", "Norco", "Norwalk", "Novato",
    "Oakdale", "Oakley", "Ojai", "Orange Cove", "Orland", "Orinda", "Oroville",
    "Pacific Grove", "Pacifica", "Palm Desert", "Palm Springs", "Palo Alto", "Palo Cedro", "Palos Verdes Estates", "Paradise", "Paramount", "Parlier", "Patterson", "Penngrove", "Perris", "Petaluma", "Pico Rivera", "Piedmont", "Pinole", "Pismo Beach", "Pittsburg", "Placentia", "Placerville", "Pleasant Hill", "Pleasanton", "Plymouth", "Point Arena", "Port Hueneme", "Porterville", "Portola", "Portola Valley", "Poway",
    "Rancho Cordova", "Rancho Mirage", "Rancho Palos Verdes", "Rancho Santa Margarita", "Red Bluff", "Redding", "Redlands", "Redondo Beach", "Redwood City", "Reedley", "Rialto", "Richmond", "Ridgecrest", "Rio Dell", "Rio Vista", "Ripon", "Riverbank", "Rocklin", "Rohnert Park", "Rolling Hills", "Rolling Hills Estates", "Ross",
    "San Anselmo", "San Ardo", "San Bruno", "San Buenaventura (Ventura)", "San Carlos", "San Clemente", "San Dimas", "San Fernando", "San Gabriel", "San Jacinto", "San Joaquin", "San Juan Bautista", "San Juan Capistrano", "San Leandro", "San Luis Obispo", "San Marcos", "San Marino", "San Mateo", "San Pablo", "San Rafael", "San Ramon", "Sand City", "Sanger", "Santa Barbara", "Santa Clara", "Santa Cruz", "Santa Fe Springs", "Santa Maria", "Santa Monica", "Santee", "Saratoga", "Sausalito", "Scotts Valley", "Seal Beach", "Seaside", "Sebastopol", "Selma", "Shafter", "Shasta Lake", "Sierra Madre", "Signal Hill", "Simi Valley", "Solana Beach", "Soledad", "Solvang", "Sonoma", "Sonora", "South El Monte", "South Gate", "South Lake Tahoe", "South Pasadena", "South San Francisco", "St. Helena", "Stanton", "Suisun City", "Susanville", "Sutter Creek",
    "Taft", "Tehachapi", "Tehama", "Temecula", "Temple City", "Thousand Oaks", "Tiburon", "Torrance", "Tracy", "Trinidad", "Truckee", "Tulare", "Tulelake", "Turlock", "Tustin", "Twentynine Palms",
    "Ukiah", "Union City", "Upland",
    "Vacaville", "Vallejo", "Vernon", "Victorville", "Villa Park", "Visalia", "Vista",
    "Walnut", "Walnut Creek", "Wasco", "Waterford", "Watsonville", "Weed", "West Covina", "West Hollywood", "West Sacramento", "Westlake Village", "Westminster", "Westmorland", "Wheatland", "Whittier", "Wildomar", "Williams", "Willits", "Willows", "Windsor", "Winterhaven", "Winters", "Woodlake", "Woodland", "Woodside",
    "Yorba Linda", "Yountville", "Yreka", "Yuba City", "Yucaipa", "Yucca Valley"
    ],
    "Texas": [
    "Houston", "San Antonio", "Dallas", "Austin", "Fort Worth", "El Paso", "Arlington", "Corpus Christi", "Plano", "Lubbock", "Laredo", "Irving", "Garland", "Frisco", "McKinney", "Grand Prairie", "Amarillo", "Brownsville", "Denton", "Killeen", "McAllen", "Mesquite", "Pasadena", "Waco", "Midland", "Lewisville", "Carrollton", "Round Rock", "Abilene", "Pearland", "College Station", "Odessa",
    "Abbott", "Abernathy", "Ackerly", "Addison", "Adrian", "Agua Dulce", "Alamo", "Alamo Heights", "Alba", "Albany", "Aledo", "Alice", "Allen", "Alma", "Alpine", "Alto", "Alton", "Alvarado", "Alvin", "Alvord", "Amherst", "Anahuac", "Andrews", "Angleton", "Anna", "Anson", "Anthony", "Anton", "Appleby", "Aquilla", "Aransas Pass", "Archer City", "Arcola", "Argyle", "Arp", "Asherton", "Aspermont", "Atascosa", "Athens", "Atlanta", "Aubrey", "Aurora", "Avery", "Avinger", "Azle",
    "Bacliff", "Bailey", "Bailey's Prairie", "Baird", "Balch Springs", "Balcones Heights", "Ballinger", "Balmorhea", "Bandera", "Bangs", "Bardwell", "Barstow", "Bartlett", "Bartonville", "Bastrop", "Bay City", "Bayou Vista", "Bayside", "Baytown", "Bayview", "Beach City", "Beacon Hill", "Bear Creek", "Beasley", "Beaumont", "Beckville", "Bedford", "Bee Cave", "Beeville", "Bellaire", "Belview", "Bellevue", "Bellmead", "Bells", "Bellville", "Belton", "Benavides", "Benbrook", "Benjamin", "Berryville", "Bertram", "Beverly Hills", "Bevil Oaks", "Big Lake", "Big Sandy", "Big Spring", "Big Wells", "Bigfoot", "Birome", "Bishop", "Bishop Hills", "Blackwell", "Blanco", "Blanket", "Blessing", "Bloomburg", "Blooming Grove", "Blossom", "Blue Mound", "Blue Ridge", "Blum", "Boerne", "Bogata", "Bonham", "Bonney", "Booker", "Borger", "Bovina", "Bowie", "Boyd", "Boys Ranch", "Brackettville", "Brady", "Brazoria", "Breckenridge", "Bremond", "Brenham", "Briar", "Briarcliff", "Briaroaks", "Bridgeport", "Broaddus", "Bronte", "Brookshire", "Brookside Village", "Browndell", "Brownfield", "Brownsboro", "Brownwood", "Bruceville-Eddy", "Bryan", "Bryson", "Buckholts", "Buda", "Buffalo", "Buffalo Gap", "Buffalo Springs", "Bullard", "Bulverde", "Bunker Hill Village", "Burkburnett", "Burke", "Burleson", "Burnet", "Burton", "Byers", "Bynum",
    "Cactus", "Caddo Mills", "Caldwell", "Callisburg", "Calver", "Calvert", "Cameron", "Camp Wood", "Campbell", "Canadian", "Caney City", "Canton", "Canutillo", "Canyon", "Carbon", "Carmine", "Carrizo Springs", "Carthage", "Castle Hills", "Castroville", "Catarina", "Cedar Hill", "Cedar Park", "Celeste", "Celina", "Center", "Centerville", "Centralia", "Chandler", "Channing", "Charlotte", "Chester", "Chico", "Childress", "Chillicothe", "China", "China Grove", "Chireno", "Christine", "Christoval", "Cibolo", "Cisco", "Clarendon", "Clarksville", "Clarksville City", "Claude", "Clear Lake Shores", "Cleburne", "Cleveland", "Clifton", "Clint", "Clute", "Clyde", "Coahoma", "Cockrell Hill", "Coffee City", "Coldspring", "Coleman", "Colleyville", "Collinsville", "Colmesneil", "Colorado City", "Columbus", "Comanche", "Combes", "Combine", "Commerce", "Como", "Concan", "Conroe", "Converse", "Cool", "Coolidge", "Cooper", "Coppell", "Copper Canyon", "Copperas Cove", "Corinth", "Corrigan", "Corsicana", "Cottonwood", "Cottonwood Shores", "Cotulla", "Cove", "Covington", "Coyanosa", "Crandall", "Crane", "Cranfills Gap", "Crawford", "Creedmoor", "Cresson", "Crockett", "Crosby", "Crosbyton", "Cross Mountain", "Cross Plains", "Cross Roads", "Crowell", "Crowley", "Crystal City", "Cuero", "Cumby", "Cuney", "Cushing", "Cut and Shoot",
    "Daingerfield", "Daisetta", "Dalhart", "Dalworthington Gardens", "Danbury", "Darrouzett", "Dawson", "Dayton", "Dayton Lakes", "De Kalb", "De Leon", "Dean", "Decatur", "Deer Park", "DeCordova", "Del Rio", "Dell City", "Delmita", "Denison", "Departure", "Deport", "Desdemona", "DeSoto", "Detroit", "Devers", "Devine", "Deweyville", "Diboll", "Dickens", "Dickinson", "Dilley", "Dimmit", "Dodd City", "Dodson", "Domino", "Donna", "Dorchester", "Double Oak", "Double Haven", "Douglassville", "Dripping Springs", "Driscoll", "Dublin", "Dumas", "Duncanville",
    "Eagle Lake", "Eagle Pass", "Early", "Earth", "East Bernard", "East Mountain", "East Tawakoni", "Eastland", "Easton", "Ector", "Edcouch", "Eden", "Edgecliff Village", "Edgewood", "Edinburg", "Edison", "Edmonson", "Edna", "Edom", "El Cenizo", "El Campo", "El Lago", "Eldorado", "Electra", "Elgin", "Elkhart", "Elmendorf", "Elmo", "Elsa", "Emhouse", "Emory", "Enloe", "Ennis", "Escobares", "Estelline", "Euless", "Eureka", "Eustace", "Evadale", "Evant", "Everman",
    "Fair Oaks Ranch", "Fairfield", "Fairview", "Falfurrias", "Falls City", "Farmers Branch", "Farmersville", "Farwell", "Fate", "Fayetteville", "Fentress", "Ferris", "Flatonia", "Florence", "Floresville", "Flower Mound", "Floydada", "Follett", "Forest Hill", "Forney", "Forsyth", "Fort Davis", "Fort Hancock", "Fort Stockton", "Fostoria", "Fowlerton", "Franklin", "Frankston", "Fredericksburg", "Freeport", "Freer", "Friendswood", "Friona", "Fritch", "Frost", "Fruitvale", "Fulshear", "Fulton",
    "Gail", "Gaines Creek", "Gainesville", "Galena Park", "Galveston", "Ganado", "Garden City", "Garden Ridge", "Garfield", "Garrison", "Gary City", "Gatesville", "George West", "Georgetown", "Gholson", "Giddings", "Gilmer", "Girard", "Gladewater", "Glazier", "Glen Rose", "Glenn Heights", "Glenwood", "Godley", "Goldsmith", "Goldthwaite", "Goliad", "Golinda", "Gonzales", "Goodlow", "Goodlow Cell", "Goodrich", "Goodnight", "Gordon", "Goree", "Gorman", "Graford", "Graham", "Granbury", "Grand Falls", "Grand Saline", "Grandview", "Granger", "Granite Shoals", "Granjeno", "Grape Creek", "Grapeland", "Grapevine", "Grayburg", "Grays Prairie", "Greenville", "Gregory", "Grey Forest", "Groesbeck", "Groom", "Groves", "Groveton", "Gruver", "Guerra", "Gun Barrel City", "Gunter", "Gustine", "Guthrie",
    "Hale Center", "Hallettsville", "Hallsburg", "Hallsville", "Haltom City", "Hamilton", "Hamlin", "Happy", "Hardin", "Harker Heights", "Harlingen", "Harper", "Hart", "Hartley", "Haskell", "Haslet", "Hawk Cove", "Hawkins", "Hawley", "Hays", "Hearne", "Heath", "Hebron", "Hedley", "Hedwig Village", "Helotes", "Hemphill", "Hempstead", "Henderson", "Henrietta", "Hereford", "Hewitt", "Hickory Creek", "Hico", "Hidalgo", "Higgins", "High Island", "Highland Haven", "Highland Park", "Highland Village", "Highlands", "Hill Country Village", "Hillcrest", "Hillcrest Village", "Hillsboro", "Hilshire Village", "Hitchcock", "Holiday Lakes", "Holland", "Holliday", "Hollywood Park", "Homer", "Hondo", "Honey Grove", "Hooks", "Horizon City", "Horseshoe Bay", "Howardwick", "Howe", "Hubbard", "Hudson", "Hudson Oaks", "Hughes Springs", "Hull", "Humble", "Hunters Creek Village", "Huntington", "Huntsville", "Hurst", "Hutchins", "Hutto", "Huxley",
    "Idalou", "Impact", "Imperial", "Indian Lake", "Industry", "Inez", "Ingleside", "Ingleside on the Bay", "Ingram", "Iola", "Iowa Park", "Iraan", "Iredell", "Italy", "Itasca", "Ivanhoe", "Ivanhoe North",
    "Jacinto City", "Jacksboro", "Jacksonville", "Jamaica Beach", "Jarrell", "Jasper", "Jayton", "Jefferson", "Jersey Village", "Jewett", "Joaquin", "Johnson City", "Jolly", "Jollyville", "Jones Creek", "Jonestown", "Josephine", "Joshua", "Jourdanton", "Junction", "Justin",
    "Karnes City", "Katy", "Kaufman", "Keene", "Keller", "Kemah", "Kemp", "Kempner", "Kendleton", "Kenedy", "Kenefick", "Kennard", "Kennedale", "Kerens", "Kermit", "Kerrville", "Kilgore", "Kingsbury", "Kingsland", "Kingston", "Kingsville", "Kirby", "Kirbyville", "Kirvin", "Kleberg", "Klein", "Knickerbocker", "Knollwood", "Knox City", "Kosse", "Kountze", "Kress", "Krugerville", "Krum", "Kurten", "Kyle",
    "La Coste", "La Feria", "La Grange", "La Grulla", "La Joya", "La Marque", "La Porte", "La Prairie", "La Pryor", "La Vernia", "La Villa", "La Ward", "Lacey-Lakeview", "Ladonia", "Lago Vista", "Laguna Vista", "Lake Dallas", "Lake Houston", "Lake Jackson", "Lake Kiowa", "Lake Bridgeport", "Lake Cisco", "Lake Creek", "Lake Tawakoni", "Lake Worth", "Lakeport", "Lakeside", "Lakeside City", "Lakeview", "Lakewood Village", "Lamar", "Lamasco", "Lamesa", "Lampasas", "Lancaster", "Laneville", "Langtry", "Lantana", "Lapis", "Latch", "Latexo", "Laughlin", "Lawn", "League City", "Leakey", "Leander", "Leary", "Lefors", "Leona", "Leonard", "Leroy", "Levelland", "Lexington", "Liberty", "Liberty Hill", "Lincoln", "Lindale", "Linden", "Lindsay", "Lingleville", "Lipan", "Lipscomb", "Little Elm", "Little River-Academy", "Littlefield", "Live Oak", "Liverpool", "Livingston", "Llano", "Lockhart", "Lockney", "Log Cabin", "Lometa", "Lone Oak", "Lone Star", "Longview", "Loop", "Lopeno", "Loraine", "Lorena", "Lorenzo", "Los Fresnos", "Los Indios", "Los Ojos", "Los Ybanez", "Lott", "Lovelady", "Lowake", "Lowry Crossing", "Lucas", "Lueders", "Lufkin", "Luling", "Lumberton", "Lyford", "Lyons", "Lytle",
    "Mabank", "Madisonville", "Magnolia", "Malakoff", "Malone", "Malta", "Manchaca", "Manor", "Mansfield", "Manvel", "Marathon", "Marble Falls", "Marfa", "Marietta", "Marion", "Markham", "Marlin", "Marquette", "Marquez", "Marshall", "Mart", "Martindale", "Mason", "Matador", "Matagorda", "Mathis", "Maud", "Mauriceville", "Maypearl", "Maysfield", "Meadow", "Meadowlakes", "Meadows Place", "Megargel", "Melissa", "Melvin", "Memphis", "Menard", "Mentone", "Mercedes", "Meridian", "Merkel", "Mertens", "Mertzon", "Mexia", "Miami", "Midkiff", "Midlothian", "Midline", "Midway", "Milano", "Milam", "Mild", "Miles", "Milford", "Miller's Cove", "Millersview", "Millican", "Millsap", "Mineola", "Mineral Wells", "Mingus", "Mirando City", "Mission", "Mission Bend", "Missouri City", "Mobeetie", "Monahans", "Mont Belvieu", "Montagu", "Montague", "Montalba", "Montgomery", "Moody", "Moore", "Moore Station", "Moran", "Morgan", "Morgan's Point", "Morgan's Point Resort", "Morse", "Morton", "Moscow", "Moulton", "Mountain City", "Mount Calm", "Mount Enterprise", "Mount Pleasant", "Mount Vernon", "Muenster", "Muleshoe", "Mullin", "Munday", "Murchison", "Murphy", "Mustang", "Mustang Ridge", "Myra", "Myrtle Springs",
    "Nacogdoches", "Naples", "Napoleon", "Nash", "Nassau Bay", "Natalia", "Navarro", "Navasota", "Nazareth", "Neches", "Nederland", "Needleville", "Needville", "Nesbitt", "Nevada", "New Berlin", "New Boston", "New Braunfels", "New Caney", "New Chapel Hill", "New Fairwood", "New Fairview", "New Hope", "New London", "New Montgomery", "New Summerfield", "New Waverly", "Newark", "Newby", "Newcastle", "Newton", "Neylandville", "Niederwald", "Nixon", "Nocona", "Nolanville", "Nome", "Noonday", "Nordheim", "Normangee", "North Cleveland", "North Richland Hills", "Northlake", "Novice", "Nursery",
    "Oak Grove", "Oak Ridge", "Oak Ridge North", "Oak Valley", "Oakwood", "O'Brien", "Odem", "Oglesby", "Oilton", "Old River-Winfree", "Oldham", "Olmito", "Olmos Park", "Olney", "Olton", "Omaha", "Onalaska", "Onion Creek", "Orange", "Orange Grove", "Orchard", "Ore City", "Overton", "Ovilla", "Oyster Creek", "Ozona",
    "Paducah", "Paint Rock", "Palacios", "Palestine", "Palm Valley", "Palmer", "Palmhurst", "Palmview", "Pampa", "Panhandle", "Panorama Village", "Pantego", "Paradise", "Paris", "Parker", "Parmer", "Paschal", "Pattison", "Patton Village", "Pawnee", "Payne Springs", "Pecan Gap", "Pecan Hill", "Pecos", "Pelican Bay", "Penelope", "Penitas", "Pennington", "Peoria", "Perryton", "Petersburg", "Petrolia", "Petronila", "Pflugerville", "Pharr", "Pilot Point", "Pine Forest", "Pine Island", "Pinehurst", "Pineland", "Piney Point Village", "Pittsburg", "Plains", "Plainview", "Pleak", "Pleasant Valley", "Pleasanton", "Plum", "Plum Grove", "Point", "Point Blank", "Point Comfort", "Point Venture", "Pollok", "Ponder", "Pontotoc", "Port Arthur", "Port Aransas", "Port Isabel", "Port Lavaca", "Port Mansfield", "Port Neches", "Port O'Connor", "Porter", "Porter Heights", "Post", "Post Oak Bend", "Poteet", "Poth", "Pottsboro", "Powell", "Poynor", "Prairie View", "Premont", "Presidio", "Price", "Priddy", "Primera", "Princeton", "Proctor", "Progreso", "Progreso Lakes", "Prosper", "Providence", "Purdon", "Putnam", "Pyote",
    "Quanah", "Queen City", "Quinlan", "Quintana", "Quitaque", "Quitman",
    "Radical", "Ralls", "Rancho Viejo", "Ranger", "Rangerville", "Rankin", "Ransom Canyon", "Ravenna", "Raymondville", "Realitos", "Red Lick", "Red Oak", "Redwater", "Reklaw", "Refugio", "Reno (Lamar County)", "Reno (Parker/Tarrant County)", "Retreat", "Rhome", "Rice", "Richardson", "Richland", "Richland Springs", "Richmond", "Richwood", "Ridge", "Ridgeway", "Riesel", "Rio Bravo", "Rio Grande City", "Rio Hondo", "Rio Vista", "Rising Star", "River Oaks", "Riverside", "Roanoke", "Roaring Springs", "Robert Lee", "Robinson", "Robstown", "Roby", "Rochester", "Rockdale", "Rockland", "Rockport", "Rocksprings", "Rockwall", "Rocky Mound", "Rogers", "Roma", "Roman Forest", "Ropesville", "Roscoe", "Rose City", "Rosebud", "Rosenberg", "Ross", "Rosser", "Rotan", "Round Top", "Rowlett", "Roxton", "Royse City", "Rule", "Runge", "Runaway Bay", "Rusk",
    "Sabinal", "Sabine Pass", "Sachse", "Sacul", "Sadler", "Sagerton", "Sagamore", "Saginaw", "Saint Jo", "Saint Hedwig", "Salado", "Saltillo", "Samnorwood", "San Augustine", "San Benito", "San Diego", "San Felipe", "San Isidro", "San Juan", "San Leanna", "San Manuel-Linn", "San Marcos", "San Perlita", "San Saba", "San Ygnacio", "Sanctuary", "Sandy Hollow-Escondido", "Sandy Oaks", "Sanger", "Sansom Park", "Santa Anna", "Santa Clara", "Santa Elena", "Santa Fe", "Santa Maria", "Santa Rosa", "Savoy", "Schertz", "Schulenburg", "Scissors", "Scotland", "Scottsville", "Scroggy", "Scurry", "Seadrift", "Seagoville", "Seagraves", "Sealy", "Seguin", "Selcer", "Selden", "Selma", "Seminole", "Serenada", "Seven Points", "Seymour", "Shady Shores", "Shafter", "Shallowater", "Shamrock", "Shavano Park", "Sheldon", "Shenandoah", "Shepherd", "Sherman", "Shiner", "Shiro", "Shoreacres", "Silsbee", "Silverton", "Simonton", "Sinton", "Sisterdale", "Slaton", "Smiley", "Smithville", "Smyer", "Snook", "Snyder", "Socorro", "Somerset", "Somervell", "Somerville", "Sonora", "Sour Lake", "South Houston", "South Mountain", "South Padre Island", "South Texas", "South Toledo Bend", "Southlake", "Southmead", "Southside Place", "Southmayd", "Southwest", "Spade", "Spicewood", "Splendora", "Spofford", "Spring", "Spring Valley Village", "Springlake", "Springtown", "Spur", "Stafford", "Stagecoach", "Stamford", "Stanton", "Staples", "Star", "Star Harbor", "Stephenville", "Sterling City", "Stinnett", "Stockdale", "Stonewall", "Stowell", "Stratford", "Strawn", "Streetman", "Study Butte", "Sudan", "Sugar Land", "Sullivan City", "Sulphur Springs", "Summerfield", "Sundown", "Sunnyvale", "Sunray", "Sunrise Beach Village", "Sunset", "Sunset Valley", "Sunview", "Sunwood", "Sweeny", "Sweetwater", "Swisher",
    "Taft", "Tahoka", "Talco", "Tatum", "Taylor", "Taylor Lake Village", "Teague", "Tehuacana", "Telferner", "Temple", "Tenaha", "Terlingua", "Terrell", "Terrell Hills", "Texarkana", "Texas City", "Texline", "The Colony", "The Woodlands", "Thicket", "Thompsons", "Thorndale", "Thornton", "Thorntonville", "Thrall", "Three Rivers", "Throckmorton", "Tiki Island", "Timbercreek Canyon", "Timpson", "Tioga", "Tira", "Tivoli", "Toco", "Todd Mission", "Tolar", "Tom Bean", "Tomball", "Tool", "Toyah", "Toyahvale", "Travis", "Trent", "Trenton", "Trinidad", "Trinity", "Trophy Club", "Troup", "Troy", "Tuleta", "Tulia", "Turkey", "Tuscola", "Tye", "Tyler", "Tynan",
    "Utopia", "Uvalde", "Utley",
    "Valentine", "Valley Mills", "Valley View", "Van", "Van Alstyne", "Van Horn", "Van Vleck", "Vanderpool", "Vega", "Venus", "Vera", "Vernon", "Victoria", "Vidor", "Villa Pancho", "Village of the Hills", "Vineyard", "Volente", "Von Ormy", "Votaw",
    "Waelder", "Wake Village", "Wall", "Waller", "Wallis", "Walnut Springs", "Warren", "Warren City", "Washburn", "Washington", "Waskom", "Watauga", "Water Valley", "Waxahachie", "Wayside", "Weatherford", "Webster", "Weches", "Weimar", "Weinert", "Weir", "Welch", "Wellington", "Wellman", "Wells", "Weslaco", "West", "West Columbia", "West Lake Hills", "West Odessa", "West Orange", "West Pampa", "West Tawakoni", "West University Place", "Westbrook", "Westlake", "Westminster", "Weston", "Westover Hills", "Westworth Village", "Wetmore", "Wharton", "Wheeler", "White Deer", "White Oak", "White Settlement", "Whiteface", "Whitehouse", "Whitesboro", "Whitewright", "Whitharral", "Whitney", "Wichita Falls", "Wickett", "Wildorado", "Willamar", "Willis", "Willow Park", "Wills Point", "Wilmer", "Wilson", "Wimberley", "Windcrest", "Windom", "Windthorst", "Winfield", "Wink", "Winnie", "Winnsboro", "Winona", "Winter Haven", "Winters", "Wixon Valley", "Wolfe City", "Wolfforth", "Woodcreek", "Woodlake", "Woodland", "Woodloch", "Woodsbend", "Woodsboro", "Woodson", "Woodville", "Woodway", "Wortham", "Wyldwood", "Wylie",
    "Yancey", "Yantis", "Yoakum", "Yorktown", "Young", "Youngsport", "Ysleta",
    "Zapata", "Zavalla", "Zephyr"
    ],
    "Florida": [
        "Jacksonville", "Miami", "Tampa", "Orlando", "St. Petersburg", "Hialeah", "Port St. Lucie", "Cape Coral", "Tallahassee", "Fort Lauderdale",
        "Pembroke Pines", "Hollywood", "Miramar", "Gainesville", "Coral Springs", "Clearwater", "Palm Bay", "Pompano Beach", "West Palm Beach", "Lakeland",
        "Miami Gardens", "Davie", "Sunrise", "Boca Raton", "Deltona", "Palm Coast", "Plantation", "Fort Myers", "Deerfield Beach", "Melbourne",
        "Largo", "Homestead", "Boynton Beach", "North Port", "Kissimmee", "Doral", "Daytona Beach", "Lauderhill", "Tamarac", "Weston",
        "Delray Beach", "Ocala", "Port Orange", "Sanford", "Wellington", "St. Cloud", "Palm Beach Gardens", "Margate", "Coconut Creek", "Bradenton"
    ],
    "New York": [
        "New York City", "Buffalo", "Rochester", "Yonkers", "Syracuse", "Albany", "New Rochelle", "Mount Vernon", "Schenectady", "Utica",
        "White Plains", "Hempstead", "Troy", "Niagara Falls", "Binghamton", "Rome", "Long Beach", "Poughkeepsie", "North Tonawanda", "Jamestown",
        "Ithaca", "Elmira", "Newburgh", "Middletown", "Auburn", "Watertown", "Glen Cove", "Saratoga Springs", "Kingston", "Peekskill",
        "Lockport", "Plattsburgh", "Cortland", "Oswego", "Batavia", "Oneonta", "Olean", "Amsterdam", "Geneva", "Canandaigua"
    ],
    "Pennsylvania": [
        "Philadelphia", "Pittsburgh", "Allentown", "Erie", "Reading", "Scranton", "Bethlehem", "Lancaster", "Harrisburg", "Altoona",
        "York", "Wilkes-Barre", "Chester", "State College", "Norristown", "Bethel Park", "Hazleton", "Easton", "Monroeville", "Williamsport",
        "Lebanon", "Pottstown", "Carlisle", "Chambersburg", "New Castle", "Murrysville", "Baldwin", "Phoenixville", "West Chester", "Lansdale"
    ],
    "Illinois": [
        "Chicago", "Aurora", "Joliet", "Naperville", "Rockford", "Elgin", "Springfield", "Peoria", "Waukegan", "Champaign",
        "Bloomington", "Cicero", "Arlington Heights", "Evanston", "Schaumburg", "Bolingbrook", "Decatur", "Palatine", "Skokie", "Des Plaines",
        "Orland Park", "Tinley Park", "Oak Lawn", "Berwyn", "Mount Prospect", "Normal", "Wheaton", "Hoffman Estates", "Downers Grove", "Elmhurst"
    ],
    "Ohio": [
        "Columbus", "Cleveland", "Cincinnati", "Toledo", "Akron", "Dayton", "Parma", "Canton", "Lorain", "Hamilton",
        "Youngstown", "Springfield", "Kettering", "Elyria", "Lakewood", "Cuyahoga Falls", "Middletown", "Euclid", "Newark", "Mansfield",
        "Mentor", "Beavercreek", "Strongsville", "Fairfield", "Cleveland Heights", "Dublin", "Warren", "Findlay", "Lancaster", "Lima"
    ],
    "Georgia": [
        "Atlanta", "Augusta", "Columbus", "Savannah", "Athens", "Sandy Springs", "South Fulton", "Roswell", "Johns Creek", "Warner Robins",
        "Albany", "Alpharetta", "Marietta", "Stonecrest", "Smyrna", "Valdosta", "Brookhaven", "Dunwoody", "Gainesville", "Newnan",
        "Mableton", "Milton", "Peachtree Corners", "Hinesville", "Douglasville", "Rome", "East Point", "Peachtree City", "Tucker", "Kennesaw"
    ],
    "North Carolina": [
        "Charlotte", "Raleigh", "Greensboro", "Durham", "Winston-Salem", "Fayetteville", "Cary", "Wilmington", "High Point", "Concord",
        "Asheville", "Gastonia", "Jacksonville", "Chapel Hill", "Apex", "Huntersville", "Burlington", "Rocky Mount", "Kannapolis", "Wake Forest",
        "Wilson", "Hickory", "Indian Trail", "Mooresville", "Greenville", "Matthews", "Monroe", "Sanford", "Thomasville", "Goldboro"
    ],
    "Michigan": [
        "Detroit", "Grand Rapids", "Warren", "Sterling Heights", "Ann Arbor", "Lansing", "Flint", "Dearborn", "Livonia", "Troy",
        "Westland", "Farmington Hills", "Kalamazoo", "Wyoming", "Southfield", "Rochester Hills", "Taylor", "Pontiac", "St. Clair Shores", "Royal Oak",
        "Dearborn Heights", "Battle Creek", "Novi", "Kentwood", "Portage", "East Lansing", "Saginaw", "Roseville", "Midland", "Muskegon"
    ],
    "New Jersey": [
        "Newark", "Jersey City", "Paterson", "Elizabeth", "Edison", "Woodbridge", "Lakewood", "Toms River", "Hamilton", "Trenton",
        "Clifton", "Camden", "Brick", "Cherry Hill", "Passaic", "Middletown", "Union City", "Old Bridge", "Bayonne", "East Orange",
        "Franklin", "North Bergen", "Vineland", "Union", "Piscataway", "New Brunswick", "Perth Amboy", "Plainfield", "West New York", "Hackensack"
    ],
    "Virginia": [
        "Virginia Beach", "Norfolk", "Chesapeake", "Richmond", "Newport News", "Alexandria", "Hampton", "Roanoke", "Portsmouth", "Suffolk",
        "Lynchburg", "Harrisonburg", "Charlottesville", "Danville", "Manassas", "Petersburg", "Fredericksburg", "Winchester", "Salem", "Staunton",
        "Fairfax", "Hopewell", "Waynesboro", "Colonial Heights", "Radford", "Leesburg", "Blacksburg", "Herndon", "Reston", "Ashburn"
    ],
    "Washington": [
        "Seattle", "Spokane", "Tacoma", "Vancouver", "Bellevue", "Kent", "Everett", "Renton", "Federal Way", "Spokane Valley",
        "Yakima", "Bellingham", "Kennewick", "Kirkland", "Pasco", "Olympia", "Auburn", "Redmond", "South Hill", "Richland",
        "Marysville", "Lakewood", "Shoreline", "Burien", "Lacey", "Sammamish", "Bothell", "Puyallup", "Edmonds", "Issaquah"
    ],
    "Arizona": [
        "Phoenix", "Tucson", "Mesa", "Chandler", "Scottsdale", "Gilbert", "Glendale", "Peoria", "Surprise", "Yuma",
        "Avondale", "Goodyear", "Flagstaff", "Buckeye", "Casas Adobes", "Queen Creek", "Maricopa", "Lake Havasu City", "Catalina Foothills", "Casa Grande",
        "Sierra Vista", "Prescott Valley", "Oro Valley", "Prescott", "Bullhead City", "Marana", "Apache Junction", "San Tan Valley", "Florence", "Nogales"
    ],
    "Massachusetts": [
        "Boston", "Worcester", "Springfield", "Cambridge", "Lowell", "Brockton", "Quincy", "Lynn", "New Bedford", "Fall River",
        "Lawrence", "Newton", "Somerville", "Framingham", "Haverhill", "Malden", "Waltham", "Brookline", "Plymouth", "Medford",
        "Taunton", "Weymouth", "Chicopee", "Revere", "Peabody", "Methuen", "Barnstable", "Pittsfield", "Attleboro", "Arlington"
    ],

    # Remaining states — practical major-city selections (they simply do not have dozens of large cities).
    "Alabama": ["Birmingham", "Huntsville", "Montgomery", "Mobile", "Tuscaloosa", "Hoover", "Auburn", "Dothan", "Madison", "Decatur", "Florence", "Prattville", "Phenix City", "Gadsden", "Vestavia Hills"],
    "Alaska": ["Anchorage", "Fairbanks", "Juneau", "Sitka", "Ketchikan", "Wasilla", "Kenai", "Kodiak", "Palmer", "Bethel", "Homer", "Seward", "Valdez", "Kotzebue", "Nome"],
    "Arkansas": ["Little Rock", "Fayetteville", "Fort Smith", "Springdale", "Jonesboro", "Rogers", "Conway", "Bentonville", "Pine Bluff", "Benton", "Hot Springs", "Sherwood", "Texarkana", "Russellville", "Bella Vista"],
    "Colorado": ["Denver", "Colorado Springs", "Aurora", "Fort Collins", "Lakewood", "Thornton", "Arvada", "Westminster", "Pueblo", "Greeley", "Centennial", "Boulder", "Longmont", "Loveland", "Castle Rock"],
    "Connecticut": ["Bridgeport", "New Haven", "Stamford", "Hartford", "Waterbury", "Norwalk", "Danbury", "New Britain", "West Hartford", "Greenwich", "Fairfield", "Hamden", "Bristol", "Meriden", "Manchester"],
    "Delaware": ["Wilmington", "Dover", "Newark", "Middletown", "Smyrna", "Milford", "Seaford", "Georgetown", "Elsmere", "New Castle", "Millsboro", "Laurel", "Harrington", "Camden", "Lewes"],
    "Hawaii": ["Honolulu", "Hilo", "Kailua", "Kaneohe", "Waipahu", "Pearl City", "Kahului", "Mililani Town", "Ewa Beach", "Kihei", "Kapolei", "Mililani Mauka", "Makakilo", "Wahiawa", "Wailuku"],
    "Idaho": ["Boise", "Meridian", "Nampa", "Idaho Falls", "Caldwell", "Pocatello", "Coeur d'Alene", "Twin Falls", "Rexburg", "Post Falls", "Eagle", "Moscow", "Kuna", "Ammon", "Chubbuck"],
    "Indiana": ["Indianapolis", "Fort Wayne", "Evansville", "South Bend", "Carmel", "Fishers", "Bloomington", "Hammond", "Lafayette", "Noblesville", "Muncie", "Greenwood", "Terre Haute", "Gary", "Kokomo"],
    "Iowa": ["Des Moines", "Cedar Rapids", "Davenport", "Sioux City", "Iowa City", "Ankeny", "West Des Moines", "Waterloo", "Council Bluffs", "Ames", "Dubuque", "Urbandale", "Cedar Falls", "Marion", "Bettendorf"],
    "Kansas": ["Wichita", "Overland Park", "Kansas City", "Olathe", "Topeka", "Lawrence", "Shawnee", "Lenexa", "Manhattan", "Salina", "Hutchinson", "Leavenworth", "Leawood", "Dodge City", "Garden City"],
    "Kentucky": ["Louisville", "Lexington", "Bowling Green", "Owensboro", "Covington", "Richmond", "Georgetown", "Florence", "Hopkinsville", "Nicholasville", "Elizabethtown", "Henderson", "Frankfort", "Jeffersontown", "Independence"],
    "Louisiana": ["New Orleans", "Baton Rouge", "Shreveport", "Lafayette", "Lake Charles", "Kenner", "Bossier City", "Monroe", "Alexandria", "Houma", "New Iberia", "Slidell", "Central", "Ruston", "Sulphur"],
    "Maine": ["Portland", "Lewiston", "Bangor", "South Portland", "Auburn", "Biddeford", "Sanford", "Brunswick", "Augusta", "Saco", "Westbrook", "Waterville", "Presque Isle", "Brewer", "Bath"],
    "Maryland": ["Baltimore", "Frederick", "Gaithersburg", "Rockville", "Bowie", "Hagerstown", "Annapolis", "College Park", "Salisbury", "Laurel", "Greenbelt", "Cumberland", "Westminster", "Hyattsville", "Takoma Park"],
    "Minnesota": ["Minneapolis", "Saint Paul", "Rochester", "Duluth", "Bloomington", "Brooklyn Park", "Plymouth", "Woodbury", "Lakeville", "Blaine", "Maple Grove", "St. Cloud", "Eagan", "Burnsville", "Coon Rapids"],
    "Mississippi": ["Jackson", "Gulfport", "Southaven", "Biloxi", "Hattiesburg", "Olive Branch", "Meridian", "Tupelo", "Greenville", "Horn Lake", "Pearl", "Madison", "Clinton", "Brandon", "Ridgeland"],
    "Missouri": ["Kansas City", "St. Louis", "Springfield", "Columbia", "Independence", "Lee's Summit", "O'Fallon", "St. Joseph", "St. Charles", "Blue Springs", "St. Peters", "Florissant", "Joplin", "Chesterfield", "Jefferson City"],
    "Montana": ["Billings", "Missoula", "Great Falls", "Bozeman", "Helena", "Butte", "Kalispell", "Havre", "Anaconda", "Miles City", "Belgrade", "Livingston", "Whitefish", "Laurel", "Lewistown"],
    "Nebraska": ["Omaha", "Lincoln", "Bellevue", "Grand Island", "Kearney", "Fremont", "Hastings", "Norfolk", "Papillion", "Columbus", "North Platte", "La Vista", "Scottsbluff", "South Sioux City", "Beatrice"],
    "Nevada": ["Las Vegas", "Henderson", "Reno", "North Las Vegas", "Sparks", "Carson City", "Fernley", "Elko", "Mesquite", "Boulder City", "Fallon", "Winnemucca", "West Wendover", "Ely", "Yerington"],
    "New Hampshire": ["Manchester", "Nashua", "Concord", "Dover", "Rochester", "Portsmouth", "Keene", "Bedford", "Derry", "Laconia", "Lebanon", "Claremont", "Somersworth", "Berlin", "Franklin"],
    "New Mexico": ["Albuquerque", "Las Cruces", "Rio Rancho", "Santa Fe", "Roswell", "Farmington", "South Valley", "Clovis", "Hobbs", "Alamogordo", "Carlsbad", "Gallup", "Los Lunas", "Sunland Park", "Deming"],
    "North Dakota": ["Fargo", "Bismarck", "Grand Forks", "Minot", "West Fargo", "Williston", "Dickinson", "Mandan", "Jamestown", "Wahpeton", "Devils Lake", "Valley City", "Minot AFB", "Grafton", "Beulah"],
    "Oklahoma": ["Oklahoma City", "Tulsa", "Norman", "Broken Arrow", "Edmond", "Lawton", "Moore", "Midwest City", "Enid", "Stillwater", "Owasso", "Muskogee", "Bartlesville", "Shawnee", "Bixby"],
    "Oregon": ["Portland", "Salem", "Eugene", "Gresham", "Hillsboro", "Beaverton", "Bend", "Medford", "Springfield", "Corvallis", "Albany", "Tigard", "Lake Oswego", "Keizer", "Grants Pass"],
    "Rhode Island": ["Providence", "Cranston", "Warwick", "Pawtucket", "East Providence", "Woonsocket", "Coventry", "Cumberland", "North Providence", "South Kingstown", "West Warwick", "Johnston", "North Kingstown", "Bristol", "Smithfield"],
    "South Carolina": ["Columbia", "Charleston", "North Charleston", "Mount Pleasant", "Rock Hill", "Greenville", "Summerville", "Goose Creek", "Sumter", "Florence", "Spartanburg", "Greer", "Hilton Head Island", "Myrtle Beach", "Aiken"],
    "South Dakota": ["Sioux Falls", "Rapid City", "Aberdeen", "Brookings", "Watertown", "Mitchell", "Yankton", "Pierre", "Huron", "Spearfish", "Vermillion", "Brandon", "Box Elder", "Sturgis", "Madison"],
    "Tennessee": ["Nashville", "Memphis", "Knoxville", "Chattanooga", "Clarksville", "Murfreesboro", "Franklin", "Jackson", "Johnson City", "Bartlett", "Hendersonville", "Kingsport", "Collierville", "Smyrna", "Cleveland"],
    "Utah": ["Salt Lake City", "West Valley City", "West Jordan", "Provo", "Orem", "Sandy", "St. George", "Ogden", "Layton", "South Jordan", "Lehi", "Millcreek", "Taylorsville", "Logan", "Murray"],
    "Vermont": ["Burlington", "South Burlington", "Rutland", "Barre", "Montpelier", "Winooski", "St. Albans", "Newport", "Vergennes"],
    "West Virginia": ["Charleston", "Huntington", "Morgantown", "Parkersburg", "Wheeling", "Martinsburg", "Weirton", "Fairmont", "Beckley", "Clarksburg", "Teays Valley", "South Charleston", "St. Albans", "Bluefield", "Vienna"],
    "Wisconsin": ["Milwaukee", "Madison", "Green Bay", "Kenosha", "Racine", "Appleton", "Waukesha", "Eau Claire", "Oshkosh", "Janesville", "West Allis", "La Crosse", "Sheboygan", "Wauwatosa", "Fond du Lac"],
    "Wyoming": ["Cheyenne", "Casper", "Laramie", "Gillette", "Rock Springs", "Sheridan", "Green River", "Evanston", "Riverton", "Jackson", "Cody", "Rawlins", "Lander", "Torrington", "Douglas"],
    "District of Columbia": ["Washington"]
}

def get_major_cities(state_name: str) -> list:
    return MAJOR_CITIES_BY_STATE.get(state_name, [])


# Well-known neighborhoods/districts inside each state's biggest metro(s) —
# NOT an exhaustive neighborhood list, just real, commonly-referenced names
# that are worth their own query variant because a company describes itself
# as being "in Wynwood" or "in Williamsburg," not just "in Miami"/"in New
# York." Scoped to CA/TX/FL/NY on purpose: those are the states with dense
# enough business coverage for neighborhood-level search to add real signal
# rather than just multiplying query count for no gain. Off by default in
# the UI ("Also search well-known metro neighborhoods") since it stacks on
# top of an already-large per-city query set.
EXTRA_METRO_AREAS_BY_STATE = {
    "California": [
        # Los Angeles
        "Downtown Los Angeles", "Hollywood", "Silver Lake", "Echo Park",
        "Koreatown", "Los Feliz", "Sherman Oaks", "Studio City",
        "North Hollywood", "Van Nuys", "Westwood", "Encino", "Boyle Heights",
        "Highland Park", "Venice",
        # San Francisco Bay Area
        "Mission District", "Castro District", "SoMa", "Nob Hill",
        "Sunset District", "Richmond District",
    ],
    "Texas": [
        # Houston
        "Downtown Houston", "Midtown Houston", "The Heights", "River Oaks",
        "Montrose", "Galleria",
        # Dallas
        "Downtown Dallas", "Deep Ellum", "Uptown Dallas", "Oak Lawn",
        "Bishop Arts District",
    ],
    "Florida": [
        # Miami
        "Brickell", "Wynwood", "Little Havana", "South Beach",
        "Coconut Grove", "Downtown Miami", "Design District",
    ],
    "New York": [
        # New York City
        "Manhattan", "Brooklyn", "Queens", "The Bronx", "Staten Island",
        "Williamsburg", "Park Slope", "Astoria", "Flushing", "Harlem",
        "Chelsea", "SoHo", "Tribeca", "Upper East Side", "Upper West Side",
        "Greenwich Village", "Bushwick", "Bay Ridge", "Forest Hills",
        "Long Island City",
    ],
}


def get_extra_metro_areas(state_name: str) -> list:
    return EXTRA_METRO_AREAS_BY_STATE.get(state_name, [])