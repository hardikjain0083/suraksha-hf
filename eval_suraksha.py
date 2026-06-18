import sys
import os
import json

# Add parent path to load custom_test_data
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from custom_test_data import CUSTOM_CIRCULARS, CUSTOM_POLICIES

from services.gap_detector import RakeKeywordExtractor, calculate_weighted_score, SIMILARITY_THRESHOLD_MATCH, SIMILARITY_THRESHOLD_PARTIAL, SIMILARITY_THRESHOLD_CLAUSE

def run_evaluation():
    rake = RakeKeywordExtractor()
    results = []
    
    for circular in CUSTOM_CIRCULARS:
        for clause in circular["clauses"]:
            clause_text = clause["text"]
            clause_kws = set(rake.extract_keywords(clause_text))
            
            best_score = 0.0
            best_policy = None
            best_clause_text = None
            
            for policy in CUSTOM_POLICIES:
                policy_text = policy["content"]
                pol_kws = set(rake.extract_keywords(policy_text))
                
                score = calculate_weighted_score(clause_text, policy_text, clause_kws, pol_kws)
                if score > best_score:
                    best_score = score
                    best_policy = policy
                    best_clause_text = policy_text
            
            # Classification logic from gap_detector.py
            gap_type = "missing_clause"
            gap_status = "confirmed"
            
            if best_score >= SIMILARITY_THRESHOLD_MATCH:
                # Directive check
                directive_mismatch = False
                lower_clause = clause_text.lower()
                lower_policy = (best_clause_text or "").lower()
                if ("shall" in lower_clause or "must" in lower_clause) and ("may" in lower_policy or "consider" in lower_policy):
                    directive_mismatch = True
                    
                if directive_mismatch:
                    gap_status = "confirmed"
                    gap_type = "insufficient_clause"
                    reason = f"Partial match found but directive is weaker than RBI requirement (overall score {best_score:.2f})."
                else:
                    gap_status = "covered"
                    reason = f"Complies with active policy: '{best_policy['title']}' (weighted score {best_score:.2f} >= {SIMILARITY_THRESHOLD_MATCH})"
            elif best_score >= SIMILARITY_THRESHOLD_PARTIAL:
                gap_status = "confirmed"
                gap_type = "insufficient_clause"
                reason = f"Policy exists but is weaker or deviates from RBI guidelines (weighted score {best_score:.2f})."
            else:
                gap_status = "confirmed"
                gap_type = "missing_clause"
                p_title = best_policy['title'] if best_policy else "None"
                reason = f"No matching compliance clause found. Highest match: '{p_title}' (weighted score {best_score:.2f} < {SIMILARITY_THRESHOLD_PARTIAL})"
                
            results.append({
                "circular_id": circular["id"],
                "clause_text": clause_text,
                "best_policy_id": best_policy["id"] if best_policy else None,
                "score": round(best_score, 4),
                "gap_status": gap_status,
                "gap_type": gap_type,
                "reason": reason
            })
            
    with open('suraksha_results.json', 'w') as f:
        json.dump(results, f, indent=4)
        
if __name__ == "__main__":
    run_evaluation()
