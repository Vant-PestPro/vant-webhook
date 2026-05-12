"""
Pest Pro Knowledge Base — Qdrant Cloud Seed Script
Run once to populate the pest_pro_knowledge collection.
Usage: python seed_knowledge.py
Requires: QDRANT_URL and QDRANT_API_KEY env vars (or hardcode below for one-time run)
"""

import os
import uuid
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct,
    CreateCollection, CollectionStatus
)
from google import genai as google_genai

QDRANT_URL = os.environ.get("QDRANT_URL", "")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

COLLECTION_NAME = "pest_pro_knowledge"
VECTOR_SIZE = 3072  # gemini-embedding-001 (3072d)
EMBED_MODEL = "gemini-embedding-001"

# ── KNOWLEDGE BASE ────────────────────────────────────────────────────────────
# Structured Pest Pro knowledge for Vapi phone agent + Pumble bot context

KNOWLEDGE = [
    {
        "topic": "general_overview",
        "title": "About Pest Pro LLC",
        "content": (
            "Pest Pro LLC is a licensed pest control company serving Central Florida since 2011. "
            "Owner: Daniel Rumsey, CPO. Office: (407) 922-2276. AI receptionist line: (689) 334-2276. "
            "Address: 3211 Vineland Rd #107, Kissimmee FL 34746. Website: pestprollc.com. "
            "FDACS License: JB304313. Liability insurance on file. "
            "Service area: Kissimmee, Orlando, Winter Garden, Clermont, St. Cloud, Windermere, Dr. Phillips, Ocoee, Davenport, and surrounding Central Florida areas. "
            "Hours: Monday through Sunday, 8 AM to 6 PM. Emergency line available 24/7/365. "
            "We do NOT do termite control — we are not licensed for termites."
        )
    },
    {
        "topic": "service_ghp",
        "title": "General Home Protection (GHP) Service",
        "content": (
            "GHP stands for General Home Protection — our standard recurring pest control service. "
            "Covers: American roaches, Palmetto bugs, Ghost ants, Interior ants, Spiders, Silverfish, Earwigs, and general crawling insects. "
            "Treatment includes interior baseboards, exterior perimeter spray, entry points, and garage. "
            "Scheduling options: Monthly ($49-$80/month), Bi-monthly ($79/visit), Quarterly ($99/visit). "
            "Price varies by property size, service frequency, and pest pressure. "
            "Initial service fee may apply depending on pest activity. "
            "Recurring GHP contracts come with a service guarantee — if pests return between scheduled visits, we come back at no charge."
        )
    },
    {
        "topic": "service_german_roach",
        "title": "German Roach Treatment (Code R)",
        "content": (
            "German Roach cleanouts are our most intensive service, also called Code R internally. "
            "German roaches breed fast — one pair can produce 300,000 offspring in a year. Heavy infestations require a multi-step elimination protocol. "
            "What we do: targeted gel bait placement in harborage areas, IGR (Insect Growth Regulator) to sterilize the population, crack and crevice treatment, and flush-out sprays as needed. "
            "Treatment protocol: initial cleanout followed by follow-up treatments every 7-14 days until cleared. Most infestations are eliminated in 2-4 visits. "
            "Pricing: starts at $120, price increases with severity and property size. Commercial properties quoted on-site. "
            "What the client needs to do before service: remove items from under the sink, clear kitchen counters, and place food in sealed containers. "
            "After treatment: expect to see dead roaches for 7-14 days as the treatment works through the population — this is normal and a sign it is working."
        )
    },
    {
        "topic": "service_rodent",
        "title": "Rodent Control (Code M)",
        "content": (
            "Rodent control covers mice and rats. Also called Code M internally. "
            "Services include: rodent inspection and assessment, snap trap placement, glue board placement, bait stations (exterior only), exclusion recommendations (sealing entry points). "
            "We do not do exclusion work directly but will recommend and quote for it. "
            "Initial rodent cleanout pricing starts around $120-$200 depending on property size and severity. "
            "Follow-up trap checks are included. "
            "For severe infestations (like commercial/healthcare facilities), we use a full IPM (Integrated Pest Management) protocol with weekly monitoring. "
            "Rodent entry points: mice can squeeze through a hole the size of a dime. Inspect around pipes, utility lines, foundation cracks, and door gaps."
        )
    },
    {
        "topic": "service_mosquito",
        "title": "Mosquito Treatment",
        "content": (
            "Mosquito treatment targets adult mosquitoes and breeding sites. "
            "We treat: yard vegetation, shrubs, flower beds, tree canopies, standing water areas, and perimeter. "
            "Treatment method: residual mist application to resting areas using EPA-registered products safe for children and pets once dry (typically 30-60 minutes). "
            "Scheduling: monthly service is most effective since mosquito populations rebound. Can also be done as one-time event treatment. "
            "Monthly mosquito service pricing: typically $60-$100/month depending on yard size. "
            "Best results: reduce standing water sources (bird baths, planters, gutters, tarps). We handle the yard — clients handle the containers. "
            "Service usually takes 20-45 minutes depending on property size."
        )
    },
    {
        "topic": "service_bee_wasp",
        "title": "Bee and Wasp Removal",
        "content": (
            "We handle bee and wasp removal with a safety-first approach. "
            "Yellow jackets: typically in-ground nests. Treatment applied directly to nest entrance at dusk when colony is inside. "
            "Wasps (paper wasps, mud daubers): nest removal and treatment. "
            "Honey bees: we treat honey bee nests but do NOT perform structural bee removal or extraction of comb. That requires a separate contractor. "
            "Pricing: one-time bee/wasp service starts at $100+ depending on nest size, location, and accessibility. "
            "Note: if bees are inside a wall or structure, we can treat but the homeowner will need a separate contractor to open the wall and remove the comb to prevent future issues."
        )
    },
    {
        "topic": "service_commercial",
        "title": "Commercial Pest Control",
        "content": (
            "We provide commercial pest control for restaurants, hotels, resorts, apartment complexes, warehouses, healthcare facilities, and property management companies. "
            "Commercial services include: routine GHP, German roach elimination, rodent IPM programs, Integrated Pest Management documentation, sight logs and service reports. "
            "Healthcare and hospitality clients: we provide full HACCP-compliant IPM programs with detailed documentation, sight logs, and trend reporting. "
            "Commercial pricing: quoted on-site based on square footage, pest pressure, frequency, and documentation requirements. Commercial accounts typically start at $125-$800+/month. "
            "We work with property management companies like ADMC, Multi Choice, La Rosa Realty and others — familiar with coordinating with property managers and tenants."
        )
    },
    {
        "topic": "pricing_overview",
        "title": "Pest Pro Pricing Guide",
        "content": (
            "General pricing reference (all quotes are estimates — final price confirmed on-site): "
            "Monthly GHP residential: $49-$80/month. "
            "Bi-monthly GHP: $79/visit. "
            "Quarterly GHP: $99/visit. "
            "One-time service (no guarantee): starting at $80. "
            "German roach cleanout (Code R): $120+ based on severity. "
            "Rodent control (Code M): $120-$200+ based on severity. "
            "Monthly mosquito treatment: $60-$100/month. "
            "Commercial accounts: quoted on-site, typically $125-$800+/month. "
            "Initial setup fees may apply for new service agreements. "
            "Payment: we accept all major credit cards, Zelle, check. Net 30 available for commercial accounts with approval. "
            "Ask about our referral program — existing customers can earn credit for referrals."
        )
    },
    {
        "topic": "treatment_prep",
        "title": "How to Prepare for Your Pest Control Service",
        "content": (
            "Before your pest control service, here is what to do: "
            "Interior treatment: remove children and pets for 1-2 hours during and after treatment. Wipe down kitchen counters before service. Remove food items from counters. Move items away from baseboards in treatment areas. "
            "German roach treatment specifically: clear under-sink areas, clear kitchen clutter, seal or refrigerate food. The more access our technician has, the more effective the treatment. "
            "Mosquito treatment: keep children and pets indoors during treatment and for 30-60 minutes after until dry. Wear closed-toe shoes when walking in treated areas. "
            "Rodent control: clear clutter from areas where traps will be placed. The cleaner the area, the more effective the trap placement. "
            "After service: expect to see pest activity for 7-14 days as products take full effect. Seeing dead insects means the treatment is working. Call us if significant activity continues past 14 days."
        )
    },
    {
        "topic": "service_guarantee",
        "title": "Service Guarantee and Callbacks",
        "content": (
            "Pest Pro stands behind our work. "
            "For customers on a recurring service plan (monthly, bi-monthly, or quarterly): if pests return between scheduled visits, call us and we will come back at no charge. "
            "Callbacks are typically scheduled within 24-48 hours. "
            "For one-time services: no guarantee included. If you want peace of mind, ask about our recurring service plans. "
            "German roach cleanouts: we follow up until the infestation is eliminated. Multiple follow-up visits are included in the cleanout price for severe infestations. "
            "If you are not satisfied with the service, call us at (407) 922-2276. We will make it right."
        )
    },
    {
        "topic": "scheduling",
        "title": "How to Schedule a Service",
        "content": (
            "To schedule pest control service with Pest Pro, call (407) 922-2276 or reach our AI receptionist at (689) 334-2276 — available 24/7. "
            "You can also visit pestprollc.com to submit a service request form. "
            "For new customers: we typically offer a free inspection or estimate before beginning service. "
            "Service windows: we schedule in 2-hour windows. Our technician will call or text when they are on the way. "
            "Urgent or same-day requests: call the office line directly at (407) 922-2276. We do our best to accommodate urgent situations. "
            "Cancellations: please give 24 hours notice if you need to reschedule."
        )
    },
    {
        "topic": "service_area_detail",
        "title": "Service Area — Central Florida",
        "content": (
            "Pest Pro serves the greater Central Florida area including: Kissimmee, Orlando, Winter Garden, Clermont, St. Cloud, Windermere, Dr. Phillips, Ocoee, Davenport, Poinciana, Celebration, Reunion, Four Corners, Winter Park, Altamonte Springs, and surrounding areas. "
            "We serve both residential and commercial properties throughout Osceola County, Orange County, and parts of Lake and Polk counties. "
            "Not sure if we service your area? Call us at (407) 922-2276 and we will let you know."
        )
    },
    {
        "topic": "why_pest_pro",
        "title": "Why Choose Pest Pro",
        "content": (
            "Pest Pro is family-owned and operated. Daniel Rumsey, the owner, is a licensed Certified Pest Operator (CPO) who personally oversees all treatments and training. "
            "All technicians are trained, insured, and operate under FDACS License JB304313. "
            "We use EPA-registered products safe for families and pets. "
            "We have 26+ five-star Google reviews and an active Facebook page. "
            "We are familiar with the Central Florida pest environment — subtropical climate means year-round pest pressure, and we know what works here. "
            "Transparent pricing — no surprise fees. We quote before we start."
        )
    },
]


def embed_text(text: str, gemini_client) -> list:
    """Embed text using gemini-embedding-001 (3072 dimensions)."""
    result = gemini_client.models.embed_content(model=EMBED_MODEL, contents=text)
    return list(result.embeddings[0].values)


def embed_query(text: str, gemini_client) -> list:
    """Embed a search query using gemini-embedding-001."""
    result = gemini_client.models.embed_content(model=EMBED_MODEL, contents=text)
    return list(result.embeddings[0].values)


def main():
    if not QDRANT_URL or not QDRANT_API_KEY:
        print("ERROR: QDRANT_URL and QDRANT_API_KEY must be set as environment variables.")
        return

    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY must be set as environment variable.")
        return

    gemini = google_genai.Client(api_key=GEMINI_API_KEY)

    print(f"Connecting to Qdrant at {QDRANT_URL[:40]}...")
    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)

    # Delete existing collection if it exists
    try:
        client.delete_collection(COLLECTION_NAME)
        print(f"Deleted existing collection: {COLLECTION_NAME}")
    except Exception:
        pass

    # Create collection
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
    )
    print(f"Created collection: {COLLECTION_NAME} ({VECTOR_SIZE}d, Cosine)")

    # Embed and upsert all knowledge entries
    points = []
    for i, entry in enumerate(KNOWLEDGE):
        print(f"Embedding [{i+1}/{len(KNOWLEDGE)}]: {entry['title']}")
        # Embed title + content together for richer representation
        doc_text = f"{entry['title']}\n\n{entry['content']}"
        vector = embed_text(doc_text, gemini)
        points.append(PointStruct(
            id=i + 1,
            vector=vector,
            payload={
                "topic": entry["topic"],
                "title": entry["title"],
                "content": entry["content"]
            }
        ))

    client.upsert(collection_name=COLLECTION_NAME, points=points)
    print(f"\n✅ Seeded {len(points)} knowledge entries into '{COLLECTION_NAME}'")

    # Quick verification
    count = client.get_collection(COLLECTION_NAME).points_count
    print(f"✅ Verified: {count} points in collection")

    # Test query
    print("\nRunning test query: 'how much does pest control cost?'")
    query_vec = embed_query("how much does pest control cost?", gemini)
    results = client.query_points(COLLECTION_NAME, query=query_vec, limit=2).points
    for r in results:
        print(f"  Score {r.score:.3f}: {r.payload['title']}")

    print("\nSeed complete. Ready to deploy.")


if __name__ == "__main__":
    main()
