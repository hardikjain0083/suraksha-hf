import re
from typing import List, Dict, Any

def extract_circular_references(text: str) -> List[str]:
    # Match RBI circular numbers or references
    pattern = r"\b((?:DBOD|DOR|DIT|DPSS|FEMA|RPCD|FIDD|DoS|DNBS|DNBR|IDMD|DOR\.STR\.REC|DoR\.FIN\.REC)(?:\.[A-Za-z0-9]+)*\.No\.[A-Za-z0-9\-\.\/]+)\b"
    matches = re.findall(pattern, text)
    
    # Also match general Circular numbers mentioned
    pattern_general = r"\bCircular\s+No\.\s*([A-Za-z0-9\-\.\/]+)\b"
    matches_gen = re.findall(pattern_general, text)
    
    return list(set(matches + matches_gen))

async def resolve_precedent_chain(text: str, db: Any, depth: int = 3) -> Dict[str, Any]:
    refs = extract_circular_references(text)
    precedent_chain = []
    policies_to_review = []
    superseded_circulars = []
    
    # Queue for breadth-first search of circular chains (up to 3 levels)
    queue = [(ref, 1) for ref in refs]
    visited = set()
    
    while queue:
        current_ref, current_depth = queue.pop(0)
        if current_ref in visited or current_depth > depth:
            continue
        visited.add(current_ref)
        
        # Query database for matching circulars by circular_number or title
        matched_circ = await db.circulars.find_one({
            "$or": [
                {"circular_number": current_ref},
                {"title": {"$regex": re.escape(current_ref), "$options": "i"}},
                {"circular_id": current_ref}
            ]
        })
        
        if matched_circ:
            circ_num = matched_circ.get("circular_number") or matched_circ["circular_id"]
            superseded_circulars.append(circ_num)
            precedent_chain.append({
                "circular_id": matched_circ["circular_id"],
                "circular_number": circ_num,
                "title": matched_circ.get("title", ""),
                "depth": current_depth
            })
            
            # Find policies affected by this older circular (from existing gaps)
            gaps_cursor = db.gap_queue.find({"circular_id": matched_circ["circular_id"]})
            async for gap in gaps_cursor:
                if gap.get("top_policy_id"):
                    policies_to_review.append({
                        "policy_id": gap["top_policy_id"],
                        "title": gap.get("top_policy_title", "")
                    })
            
            # Trace further: see if this circular superseded others
            for sub_ref in matched_circ.get("supersedes_circulars", []):
                queue.append((sub_ref, current_depth + 1))
                
    # Unique policies list
    unique_policies = []
    seen_pol = set()
    for p in policies_to_review:
        if p["policy_id"] not in seen_pol:
            seen_pol.add(p["policy_id"])
            unique_policies.append(p)
            
    return {
        "precedent_chain": precedent_chain,
        "policies_to_review": unique_policies,
        "superseded_circulars": superseded_circulars
    }
