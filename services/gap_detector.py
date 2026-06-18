import re
import uuid
import time
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from models.gap import GapCheckResult, GapDetectionResponse, GapQueueEntry, PolicyMatch, JudgeExplanationStep
from config import MATCH_THRESHOLD, PARTIAL_THRESHOLD

# Import the new DERECHA engine we just copied over
from services.derecha_engine import GapDetector, ExtractedGuideline, ExtractedClause

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
#  Helper Functions
# ─────────────────────────────────────────────────────────────
def normalize_guideline(text: str) -> str:
    text = re.sub(r'Circular No\.\s*[A-Z0-9\-/]+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'dated\s+[0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4}', '', text, flags=re.IGNORECASE)
    text = re.sub(r'page\s+\d+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip().lower()
    return text

def compute_substance_hash(text: str) -> str:
    normalized = normalize_guideline(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

# ─────────────────────────────────────────────────────────────
#  Severity & Applicability
# ─────────────────────────────────────────────────────────────
def parse_severity_and_deadline(text: str) -> tuple[str, int]:
    severity = "medium"
    days = 14
    lower_text = text.lower()
    
    if "with immediate effect" in lower_text or "effective from date of circular" in lower_text:
        severity = "critical"
        days = 3
    elif "within 30 days" in lower_text or "compliance within 30 days" in lower_text:
        severity = "critical"
        days = 30
    elif "within 90 days" in lower_text or "quarterly" in lower_text:
        severity = "high"
        days = 90
    elif "within 180 days" in lower_text or "may consider" in lower_text or "advised" in lower_text:
        severity = "low"
        days = 180
    elif "within 1 year" in lower_text or "annual" in lower_text:
        severity = "medium"
        days = 365
        
    if severity == "medium":
        if "shall" in lower_text or "must" in lower_text or "directed to" in lower_text:
            severity = "critical"
            days = 30
        elif "should" in lower_text or "maintain" in lower_text:
            severity = "high"
            days = 90
        elif "may" in lower_text or "recommended" in lower_text:
            severity = "low"
            days = 180
            
    return severity, days

# ─────────────────────────────────────────────────────────────
#  Routing Disambiguation
# ─────────────────────────────────────────────────────────────
DEPT_KEYWORDS = {
    "DEPT-COMPLIANCE": ["kyc", "aml", "cft", "pmla", "customer identification", "due diligence", "suspicious transaction", "str"],
    "DEPT-IT-CYBER": ["cybersecurity", "information security", "firewall", "encryption", "access control", "vulnerability", "penetration testing", "iso 27001", "mfa", "multi-factor authentication", "authentication", "login", "password"],
    "DEPT-RISK": ["npa", "capital adequacy", "crar", "stress testing", "credit risk", "market risk", "operational risk", "basel"],
    "DEPT-FINANCE": ["capital", "dividend", "reserves", "provisioning", "npa classification", "income recognition", "asset classification"],
    "DEPT-OPS": ["customer grievance", "ombudsman", "turnaround time", "tat", "service standards", "branch operations", "cash management"],
    "DEPT-SME-CREDIT": ["loan", "credit appraisal", "sanction", "disbursement", "collateral", "margin", "exposure limit", "concentration risk"],
    "DEPT-HR": ["fit and proper", "director", "board", "kmp", "remuneration", "training", "certification"]
}

def route_department(circular_number: str, text: str, category_tag: str = "") -> tuple[str, bool, List[str]]:
    from config import CIRCULAR_PREFIX_MAP
    prefix_match = None
    for prefix in CIRCULAR_PREFIX_MAP:
        if circular_number.startswith(prefix) or prefix in circular_number:
            prefix_match = CIRCULAR_PREFIX_MAP[prefix]
            break
            
    dept_hints = []
    if prefix_match:
        dept_hints = prefix_match["dept_hint"]
        
    name_to_id = {
        "Compliance": "DEPT-COMPLIANCE", "IT Security": "DEPT-IT-CYBER", "Risk Management": "DEPT-RISK",
        "Finance": "DEPT-FINANCE", "Operations": "DEPT-OPS", "Credit": "DEPT-SME-CREDIT", "HR": "DEPT-HR"
    }
    
    text_lower = text.lower()
    scores = {}
    for dept_id, kws in DEPT_KEYWORDS.items():
        score = sum(text_lower.count(kw) for kw in kws)
        friendly_name = next((name for name, d_id in name_to_id.items() if d_id == dept_id), None)
        if friendly_name and friendly_name in dept_hints:
            score += 10
        scores[dept_id] = score
        
    sorted_depts = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_dept_id, top_score = sorted_depts[0]
    second_dept_id, second_score = sorted_depts[1] if len(sorted_depts) > 1 else (None, 0)
    
    is_ambiguous = False
    ambiguous_departments = []
    
    if second_dept_id and top_score > 0:
        diff_pct = (top_score - second_score) / top_score
        if diff_pct < 0.10 or (top_score == second_score and top_score > 0):
            is_ambiguous = True
            ambiguous_departments = [top_dept_id, second_dept_id]
            
    return top_dept_id, is_ambiguous, ambiguous_departments

# ─────────────────────────────────────────────────────────────
#  Auto-Assignment with Availability Checks
# ─────────────────────────────────────────────────────────────
async def get_available_employee(db: Any, department_id: str) -> Optional[str]:
    employees_cursor = db.users.find({
        "department_id": department_id,
        "role": "employee",
        "status": "active"
    })
    employees = await employees_cursor.to_list(length=100)
    if not employees:
        return None

    available = [e for e in employees if e.get("availability_status", "available") == "available" and e.get("active_gap_count", 0) < e.get("max_concurrent_gaps", 5)]
    if not available:
        return None
        
    available.sort(key=lambda e: (e.get("active_gap_count", 0), e.get("last_assigned_date") or datetime.min))
    return available[0]["emp_id"]

# ─────────────────────────────────────────────────────────────
#  Engine Core Detection Execution
# ─────────────────────────────────────────────────────────────
async def detect_gaps_for_circular(circular_id: str, db: Any) -> GapDetectionResponse:
    start = time.time()
    circular = await db.circulars.find_one({"circular_id": circular_id})
    if not circular:
        raise ValueError(f"Circular '{circular_id}' not found")
        
    if circular.get("ingestion_status") in ("not_applicable", "no_action_required"):
        return GapDetectionResponse(
            circular_id=circular_id,
            status="completed",
            reason=f"Ignored because ingestion status is {circular.get('ingestion_status')}"
        )

    title = circular.get("title", "Regulatory Circular")
    circular_num = circular.get("circular_number") or circular_id
    clauses = circular.get("clauses", [])
    
    target_clauses = [c for c in clauses if c.get("obligation_type") in ("shall", "must", "should")]
    if not target_clauses:
        target_clauses = clauses

    today = datetime.utcnow()
    policies = await db.policies.find({
        "status": "active",
        "$or": [
            {"effective_until": None},
            {"effective_until": {"$gte": today}}
        ]
    }).to_list(length=100)

    detector = GapDetector()
    results = []
    queue_entries = []
    counters = {"covered": 0, "suspected": 0, "confirmed": 0, "data_error": 0}

    for clause in target_clauses:
        clause_text = clause.get("text", "")
        clause_num = clause.get("clause_number")
        page_num = clause.get("page_number", 1)
        
        if not clause_text.strip():
            counters["data_error"] += 1
            results.append(GapCheckResult(
                clause_number=clause_num, clause_text="[empty]",
                gap_status="data_error", classification_reason="Empty clause text"
            ))
            continue
            
        frame = detector.frame_extractor.extract_frame(clause_text)
        keywords = [kw for kw, score in detector.rake.extract(clause_text, top_k=10)]
        directive_type = detector._classify_directive_type(clause_text)
        guideline = ExtractedGuideline(
            guideline_id=str(clause_num), text=clause_text, page_number=page_num,
            semantic_frame=frame, keywords=keywords, directive_type=directive_type
        )
        
        best_score = 0.0
        best_policy = None
        best_clause_text = None
        best_clause_page = 1
        matches_above_threshold = []
        best_signals = {}
        best_policy_frame = None
        
        for policy in policies:
            policy_text = policy.get("content", "")
            policy_id = policy["policy_id"]
            policy_title = policy.get("title", "")
            policy_clauses = policy.get("clauses", [])
            
            if policy_clauses:
                for pc in policy_clauses:
                    pc_text = pc.get("text_content", "")
                    pol_frame = detector.frame_extractor.extract_frame(pc_text)
                    pol_keywords = [kw for kw, score in detector.rake.extract(pc_text, top_k=10)]
                    pol_clause = ExtractedClause(
                        clause_id=policy_id, text=pc_text, page_number=pc.get("page_number", 1),
                        semantic_frame=pol_frame, keywords=pol_keywords
                    )
                    
                    signals = detector.compute_similarity_signals(guideline, pol_clause)
                    score = detector.compute_final_score(signals)
                    
                    if score > best_score:
                        best_score = score
                        best_policy = policy
                        best_clause_text = pc_text
                        best_clause_page = pc.get("page_number", 1)
                        best_line_num = None
                        best_signals = signals
                        best_policy_frame = pol_frame
                        
                    if score >= PARTIAL_THRESHOLD:
                        matches_above_threshold.append({
                            "policy_id": policy_id, "policy_title": policy_title,
                            "clause_text": pc_text, "page_number": pc.get("page_number", 1), "score": score
                        })
            else:
                pol_frame = detector.frame_extractor.extract_frame(policy_text)
                pol_keywords = [kw for kw, score in detector.rake.extract(policy_text, top_k=10)]
                pol_clause = ExtractedClause(
                    clause_id=policy_id, text=policy_text, page_number=1,
                    semantic_frame=pol_frame, keywords=pol_keywords
                )
                signals = detector.compute_similarity_signals(guideline, pol_clause)
                score = detector.compute_final_score(signals)
                
                if score > best_score:
                    best_score = score
                    best_policy = policy
                    best_clause_text = policy_text[:300]
                    best_clause_page = 1
                    best_line_num = 1
                    best_signals = signals
                    best_policy_frame = pol_frame
                    
                if score >= PARTIAL_THRESHOLD:
                    matches_above_threshold.append({
                        "policy_id": policy_id, "policy_title": policy_title,
                        "clause_text": policy_text[:300], "page_number": 1, "score": score
                    })

        if best_score >= MATCH_THRESHOLD:
            directive_keyword_mismatch = False
            lower_clause = clause_text.lower()
            lower_policy = (best_clause_text or "").lower()
            if ("shall" in lower_clause or "must" in lower_clause) and ("may" in lower_policy or "consider" in lower_policy):
                directive_keyword_mismatch = True
                
            if directive_keyword_mismatch:
                gap_status = "confirmed"
                gap_type = "insufficient_clause"
                reason = f"Partial match found but directive is weaker than RBI requirement (overall score {best_score:.2f})."
                counters["confirmed"] += 1
            else:
                gap_status = "covered"
                gap_type = "none"
                reason = f"Complies with active policy: '{best_policy['title']}' (weighted score {best_score:.2f} >= {MATCH_THRESHOLD})"
                counters["covered"] += 1
        elif best_score >= PARTIAL_THRESHOLD:
            gap_status = "confirmed"
            gap_type = "insufficient_clause"
            reason = f"Policy exists but is weaker or deviates from RBI guidelines (weighted score {best_score:.2f})."
            counters["confirmed"] += 1
        else:
            gap_status = "confirmed"
            gap_type = "missing_clause"
            p_title = best_policy['title'] if best_policy else "None"
            reason = f"No matching compliance clause found. Highest match: '{p_title}' (weighted score {best_score:.2f} < {PARTIAL_THRESHOLD})"
            counters["confirmed"] += 1
            
        result = GapCheckResult(
            clause_number=clause_num, clause_text=clause_text,
            obligation_type=clause.get("obligation_type"), severity=clause.get("severity"),
            gap_status=gap_status, similarity_score=round(best_score, 4), classification_reason=reason,
            similarity_scores=best_signals,
            rbi_action=frame.action if frame else None,
            rbi_modality=frame.modality if frame else None,
            policy_action=best_policy_frame.action if best_policy_frame else None,
            policy_modality=best_policy_frame.modality if best_policy_frame else None,
            mismatch_description=detector._generate_mismatch_description(guideline, ExtractedClause("", best_clause_text or "", 1, semantic_frame=best_policy_frame), gap_type, best_score) if gap_status != "covered" else ""
        )
        results.append(result)

        await db.circulars.update_one(
            {"circular_id": circular_id, "clauses.text": clause_text},
            {"$set": {"clauses.$.gap_status": gap_status}}
        )

        if gap_status == "confirmed":
            substance_hash = compute_substance_hash(clause_text)
            existing_gap = await db.gap_queue.find_one({
                "guideline_substance_hash": substance_hash,
                "triage_status": {"$in": ["assigned", "open"]}
            })
            if existing_gap:
                await db.gap_queue.update_one(
                    {"gap_id": existing_gap["gap_id"]},
                    {"$addToSet": {"page_numbers_list": page_num}}
                )
                continue
                
            severity, due_days = parse_severity_and_deadline(clause_text)
            dept_id, is_ambiguous, ambiguous_depts = route_department(circular_num, clause_text)
            dept_doc = await db.departments.find_one({"department_id": dept_id})
            hod_id = dept_doc.get("head_employee_id", "EMP-COMPLIANCE-HEAD") if dept_doc else "EMP-COMPLIANCE-HEAD"
            employee_id = await get_available_employee(db, dept_id)
            due_date = datetime.utcnow() + timedelta(days=due_days)
            gap_id = f"GAP-{str(uuid.uuid4())[:8].upper()}"
            
            pol_title = best_policy["title"] if best_policy else "N/A"
            c_text_preview = (best_clause_text[:100] + "...") if best_clause_text else "N/A"
            mismatch_desc = (
                f"RBI Circular {title}, Page {page_num} mandates: '{clause_text}'. "
                f"Current Bank Policy '{pol_title}', Page {best_clause_page} states: '{c_text_preview}'. "
                f"GAP: {gap_type.upper()}. Department: {dept_doc.get('name', 'Compliance') if dept_doc else 'Compliance'} must address this."
            )
            
            parent_guideline_id = None
            if len(matches_above_threshold) > 1:
                parent_guideline_id = gap_id
                
            entry = {
                "gap_id": gap_id, "circular_id": circular_id, "circular_title": title,
                "clause_number": clause_num, "clause_text": clause_text,
                "obligation_type": clause.get("obligation_type"), "severity": severity,
                "gap_status": gap_status, "similarity_score": round(best_score, 4),
                "top_policy_id": best_policy["policy_id"] if best_policy else "POL-COMP-001",
                "top_policy_title": best_policy["title"] if best_policy else "Compliance Policy",
                "classification_reason": reason, "routing": "auto_routed",
                "triage_status": "assigned" if employee_id else "open",
                "created_at": datetime.utcnow(), "page_number": page_num,
                "matched_policy_line_num": best_line_num if 'best_line_num' in locals() else None,
                "page_numbers_list": [page_num], "department_id": dept_id,
                "assigned_hod": hod_id, "assigned_employee": employee_id,
                "due_date": due_date, "fixed_policy_content": None, "remaining_gaps": [],
                "is_fixed": False, "guideline_substance_hash": substance_hash,
                "parent_guideline_id": parent_guideline_id, "source": "circular_upload",
                "is_ambiguous": is_ambiguous, "ambiguous_departments": ambiguous_depts,
                "mismatch_description": mismatch_desc,
                "similarity_scores": best_signals,
                "rbi_action": frame.action if frame else None,
                "rbi_modality": frame.modality if frame else None,
                "policy_action": best_policy_frame.action if best_policy_frame else None,
                "policy_modality": best_policy_frame.modality if best_policy_frame else None
            }
            queue_entries.append(entry)
            
            if employee_id:
                await db.users.update_one(
                    {"emp_id": employee_id},
                    {"$inc": {"active_gap_count": 1}, "$set": {"last_assigned_date": datetime.utcnow()}}
                )
                await db.notifications.insert_one({
                    "notification_id": f"NOTIF-{str(uuid.uuid4())[:8].upper()}",
                    "user_id": employee_id, "title": "New Gap Assigned",
                    "message": f"Gap {gap_id} (Page {page_num}) has been assigned to you. Due by {due_date.strftime('%Y-%m-%d')}.",
                    "type": "gap_assigned", "gap_id": gap_id, "is_read": False, "created_at": datetime.utcnow()
                })
                
            await db.notifications.insert_one({
                "notification_id": f"NOTIF-{str(uuid.uuid4())[:8].upper()}",
                "user_id": hod_id, "title": "New Departmental Gap",
                "message": f"Gap {gap_id} has been auto-routed to your department. " + (f"Assigned to employee {employee_id}." if employee_id else "No employees available - manual assignment required."),
                "type": "gap_assigned", "gap_id": gap_id, "is_read": False, "created_at": datetime.utcnow()
            })

    if queue_entries:
        await db.gap_queue.insert_many(queue_entries)

    total = len(results)
    covered = counters["covered"]
    duration_ms = int((time.time() - start) * 1000)

    return GapDetectionResponse(
        circular_id=circular_id, total_clauses_analyzed=total, covered=covered, suspected=0,
        confirmed=counters["confirmed"], data_errors=counters["data_error"],
        coverage_rate=round(covered / total, 4) if total > 0 else 0.0,
        gaps=results, detection_time_ms=duration_ms, status="completed"
    )

# ─────────────────────────────────────────────────────────────
#  Regression & Recheck Verification Pipeline
# ─────────────────────────────────────────────────────────────
async def recheck_policy_gap(gap_id: str, updated_content: str, db: Any) -> Dict[str, Any]:
    gap = await db.gap_queue.find_one({"gap_id": gap_id})
    if not gap:
        return {"resolved": False, "remaining_gaps": ["Gap not found."]}

    clause_text = gap.get("clause_text", "")
    
    detector = GapDetector()
    frame = detector.frame_extractor.extract_frame(clause_text)
    keywords = [kw for kw, score in detector.rake.extract(clause_text, top_k=10)]
    directive_type = detector._classify_directive_type(clause_text)
    guideline = ExtractedGuideline(
        guideline_id="recheck", text=clause_text, page_number=1,
        semantic_frame=frame, keywords=keywords, directive_type=directive_type
    )
    
    updated_sentences = []
    try:
        from services.watcher import parse_clauses
        parsed_c = parse_clauses(updated_content)
        updated_sentences.extend([c.text for c in parsed_c if c.text.strip()])
    except Exception:
        pass
        
    for part in re.split(r'\.\s+|\n+', updated_content):
        if part.strip():
            updated_sentences.append(part.strip())
            
    updated_sentences.append(updated_content.strip())
    
    best_score = 0.0
    for sentence in set(updated_sentences):
        pol_frame = detector.frame_extractor.extract_frame(sentence)
        pol_keywords = [kw for kw, score in detector.rake.extract(sentence, top_k=10)]
        pol_clause = ExtractedClause(clause_id="upd", text=sentence, page_number=1, semantic_frame=pol_frame, keywords=pol_keywords)
        
        signals = detector.compute_similarity_signals(guideline, pol_clause)
        s_score = detector.compute_final_score(signals)
        if s_score > best_score:
            best_score = s_score
            
    score = best_score
    original_resolved = score >= MATCH_THRESHOLD
    
    today = datetime.utcnow()
    active_circulars = await db.circulars.find({"ingestion_status": {"$in": ["fully_parsed", "partially_parsed"]}}).to_list(length=100)
    
    introduced_gaps = []
    for circ in active_circulars:
        circ_clauses = circ.get("clauses", [])
        for cc in circ_clauses:
            if cc.get("obligation_type") not in ("shall", "must", "should"):
                continue
            cc_text = cc.get("text", "")
            if cc_text == clause_text:
                continue
                
            cc_frame = detector.frame_extractor.extract_frame(cc_text)
            cc_kws = [kw for kw, score in detector.rake.extract(cc_text, top_k=10)]
            cc_dir = detector._classify_directive_type(cc_text)
            cc_guideline = ExtractedGuideline("cc", text=cc_text, page_number=1, semantic_frame=cc_frame, keywords=cc_kws, directive_type=cc_dir)
            
            best_cc_score = 0.0
            for sentence in set(updated_sentences):
                pol_frame = detector.frame_extractor.extract_frame(sentence)
                pol_keywords = [kw for kw, score in detector.rake.extract(sentence, top_k=10)]
                pol_clause = ExtractedClause(clause_id="upd", text=sentence, page_number=1, semantic_frame=pol_frame, keywords=pol_keywords)
                signals = detector.compute_similarity_signals(cc_guideline, pol_clause)
                s_score = detector.compute_final_score(signals)
                if s_score > best_cc_score:
                    best_cc_score = s_score
            
            cc_score = best_cc_score
            if cc_score < PARTIAL_THRESHOLD:
                introduced_gaps.append({
                    "circular_id": circ["circular_id"],
                    "circular_title": circ.get("title", ""),
                    "clause_text": cc_text,
                    "score": cc_score
                })

    employee_id = gap.get("assigned_employee")
    employee_doc = await db.users.find_one({"emp_id": employee_id}) if employee_id else None
    employee_name = employee_doc.get("name", "Employee") if employee_doc else "Employee"

    if original_resolved and not introduced_gaps:
        await db.gap_queue.update_one(
            {"gap_id": gap_id},
            {
                "$set": {
                    "triage_status": "resolved",
                    "fixed_policy_content": updated_content,
                    "is_fixed": True,
                    "remaining_gaps": [],
                    "similarity_score": round(score, 4),
                    "classification_reason": f"Resolved: Guideline matches updated policy content (score {score:.2f} >= {MATCH_THRESHOLD})"
                }
            }
        )
        
        if employee_id:
            await db.users.update_one(
                {"emp_id": employee_id},
                {"$inc": {"active_gap_count": -1}}
            )

        await db.notifications.insert_one({
            "notification_id": f"NOTIF-{str(uuid.uuid4())[:8].upper()}",
            "user_id": gap.get("assigned_hod"),
            "title": "Gap Resolved",
            "message": f"Gap {gap_id} has been resolved by {employee_name} in Policy '{gap.get('top_policy_title')}'. Pending review.",
            "type": "gap_resolved",
            "gap_id": gap_id,
            "is_read": False,
            "created_at": datetime.utcnow()
        })

        await db.notifications.insert_one({
            "notification_id": f"NOTIF-{str(uuid.uuid4())[:8].upper()}",
            "user_id": "EMP-ADMIN-001",
            "title": "Core Policy Update Awaiting HOD Approval",
            "message": f"Gap {gap_id} has been resolved by {employee_name}. Approve to apply to core bank policies.",
            "type": "gap_resolved",
            "gap_id": gap_id,
            "is_read": False,
            "created_at": datetime.utcnow()
        })

        return {"resolved": True, "status": "resolved", "remaining_gaps": []}
        
    elif original_resolved and introduced_gaps:
        await db.gap_queue.update_one(
            {"gap_id": gap_id},
            {
                "$set": {
                    "triage_status": "resolved",
                    "fixed_policy_content": updated_content,
                    "is_fixed": True,
                    "remaining_gaps": [f"Regression: introduces gaps on other circulars"]
                }
            }
        )
        
        if employee_id:
            await db.users.update_one(
                {"emp_id": employee_id},
                {"$inc": {"active_gap_count": -1}}
            )

        new_gaps = []
        for reg in introduced_gaps:
            reg_gap_id = f"GAP-{str(uuid.uuid4())[:8].upper()}"
            reg_substance_hash = compute_substance_hash(reg["clause_text"])
            reg_entry = {
                "gap_id": reg_gap_id,
                "circular_id": reg["circular_id"],
                "circular_title": reg["circular_title"],
                "clause_text": reg["clause_text"],
                "severity": "high",
                "gap_status": "confirmed",
                "similarity_score": round(reg["score"], 4),
                "top_policy_id": gap.get("top_policy_id"),
                "top_policy_title": gap.get("top_policy_title"),
                "classification_reason": f"Regression: introduced by fixing {gap_id}",
                "routing": "auto_routed",
                "triage_status": "assigned" if employee_id else "open",
                "created_at": datetime.utcnow(),
                "page_number": 1,
                "page_numbers_list": [1],
                "department_id": gap.get("department_id"),
                "assigned_hod": gap.get("assigned_hod"),
                "assigned_employee": employee_id,
                "due_date": datetime.utcnow() + timedelta(days=7),
                "fixed_policy_content": None,
                "remaining_gaps": [],
                "is_fixed": False,
                "guideline_substance_hash": reg_substance_hash,
                "parent_guideline_id": gap_id,
                "source": "fix_regression",
                "is_ambiguous": False,
                "ambiguous_departments": [],
                "mismatch_description": f"REGRESSION: Fixing Gap {gap_id} broke guideline compliance for circular {reg['circular_title']}."
            }
            new_gaps.append(reg_entry)
            
            if employee_id:
                await db.users.update_one(
                    {"emp_id": employee_id},
                    {"$inc": {"active_gap_count": 1}}
                )
                
        if new_gaps:
            await db.gap_queue.insert_many(new_gaps)
            
        await db.notifications.insert_one({
            "notification_id": f"NOTIF-{str(uuid.uuid4())[:8].upper()}",
            "user_id": gap.get("assigned_hod"),
            "title": "Regressions Detected in Policy Fix",
            "message": f"Gap {gap_id} fix resolved the issue but introduced {len(new_gaps)} new regressions. Created regression tasks.",
            "type": "fix_rejected",
            "gap_id": gap_id,
            "is_read": False,
            "created_at": datetime.utcnow()
        })

        return {"resolved": True, "status": "resolved_with_regression", "new_gaps": new_gaps, "remaining_gaps": []}
        
    else:
        mismatch_msg = f"The updated policy still does not comply with the circular guideline (score: {score:.2f} < {MATCH_THRESHOLD})."
        await db.gap_queue.update_one(
            {"gap_id": gap_id},
            {
                "$set": {
                    "fixed_policy_content": updated_content,
                    "is_fixed": False,
                    "remaining_gaps": [mismatch_msg]
                }
            }
        )
        
        extended_due = (gap.get("due_date") or datetime.utcnow()) + timedelta(days=3)
        await db.gap_queue.update_one(
            {"gap_id": gap_id},
            {"$set": {"due_date": extended_due}}
        )

        if employee_id:
            await db.notifications.insert_one({
                "notification_id": f"NOTIF-{str(uuid.uuid4())[:8].upper()}",
                "user_id": employee_id,
                "title": "Fix Rejected - Gap Still Active",
                "message": f"Submitting fixes for Gap {gap_id} failed. Some mismatches are still present. New deadline: {extended_due.strftime('%Y-%m-%d')}.",
                "type": "fix_rejected",
                "gap_id": gap_id,
                "is_read": False,
                "created_at": datetime.utcnow()
            })

        return {"resolved": False, "status": "failed", "remaining_gaps": [mismatch_msg]}
