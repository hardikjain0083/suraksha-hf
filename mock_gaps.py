import asyncio
from database import db

from database import db, connect_to_mongo

async def backfill_gaps():
    await connect_to_mongo()
    print("Backfilling existing gaps with mock semantic scores...")
    cursor = db.client.suraksha_maps.gap_queue.find({"similarity_scores": {"$exists": False}})
    updated = 0
    async for gap in cursor:
        gap_id = gap["gap_id"]
        # Create some realistic-looking semantic scores based on the old overall score
        score = gap.get("similarity_score", 0.6)
        mock_signals = {
            "EMBEDDING": min(score + 0.1, 0.99),
            "FRAME": score,
            "FUZZY": min(score + 0.05, 0.95),
            "JACCARD": score - 0.05,
            "TFIDF": min(score + 0.15, 0.98)
        }
        await db.client.suraksha_maps.gap_queue.update_one(
            {"_id": gap["_id"]},
            {
                "$set": {
                    "similarity_scores": mock_signals,
                    "rbi_action": "implement",
                    "rbi_modality": "must",
                    "policy_action": "consider",
                    "policy_modality": "may",
                    "mismatch_description": "Mock description: " + gap.get("classification_reason", "")
                }
            }
        )
        updated += 1
    print(f"Successfully updated {updated} existing gaps.")

if __name__ == "__main__":
    asyncio.run(backfill_gaps())
