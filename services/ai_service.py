import os
import json
import urllib.request
import urllib.error
import logging
import asyncio
from datetime import datetime
from typing import Any, Optional, Dict, List, Tuple
from config import settings

logger = logging.getLogger(__name__)

DEPARTMENTS_MAP = {
    "compliance": "DEPT-COMPLIANCE",
    "legal": "DEPT-LEGAL",
    "risk": "DEPT-RISK",
    "operations": "DEPT-OPS",
    "branch banking": "DEPT-BRANCH-BANKING",
    "it / cybersecurity": "DEPT-IT-CYBER",
    "finance / accounts": "DEPT-FINANCE",
    "hr": "DEPT-HR",
    "recovery / collections": "DEPT-RECOVERY",
    "treasury": "DEPT-TREASURY",
    "sme / retail / credit": "DEPT-SME-CREDIT",
    "security / vigilance": "DEPT-SECURITY-VIGILANCE",
    "customer service": "DEPT-CUSTOMER-SERVICE",
    "mis / reporting": "DEPT-MIS",
    "audit / inspection": "DEPT-AUDIT"
}

DEPARTMENTS_REVERSE = {v: k for k, v in DEPARTMENTS_MAP.items()}

async def call_gemini(prompt: str, json_mode: bool = False) -> str:
    key = getattr(settings, "gemini_api_key", None) or os.environ.get("GEMINI_API_KEY")
    if not key or key == "your_gemini_api_key":
        logger.warning("GEMINI_API_KEY is not configured or is default. Falling back.")
        raise ValueError("GEMINI_API_KEY not set")
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={key}"
    
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ]
    }
    
    if json_mode:
        payload["generationConfig"] = {
            "responseMimeType": "application/json"
        }
        
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        
        loop = asyncio.get_event_loop()
        def _send():
            with urllib.request.urlopen(req, timeout=12) as response:
                return response.read().decode("utf-8")
        res_body = await loop.run_in_executor(None, _send)
        res_json = json.loads(res_body)
        
        text = res_json["candidates"][0]["content"]["parts"][0]["text"]
        return text.strip()
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}")
        raise

def normalize_dept_id(dept_name: str) -> str:
    """Resolve fuzzy department names returned by Gemini to correct department ID."""
    name_clean = dept_name.lower().replace("_", " ").strip()
    # Direct match check
    if name_clean in DEPARTMENTS_MAP:
        return DEPARTMENTS_MAP[name_clean]
    # Substring checks
    for key, value in DEPARTMENTS_MAP.items():
        if key in name_clean or name_clean in key:
            return value
    return "DEPT-COMPLIANCE"  # default fallback

async def classify_department(gap_description: str, circular_title: str, circular_content: str) -> Dict[str, Any]:
    """
    Agent 1 & Agent 2: Department Classification & Cross-Department Detection.
    Returns: {
      "department_id": str,
      "confidence_score": float,
      "reasoning": str,
      "multi_department": bool,
      "affected_departments": [{"department_id": str, "severity": str}]
    }
    """
    prompt = f"""You are a bank compliance expert. Given this regulatory gap description and circular excerpt, determine which department should handle it.
Gap Description: {gap_description}
Circular Title: {circular_title}
Circular Excerpt: {circular_content[:1500]}

Options for departments: Compliance, Legal, Risk, Operations, Branch Banking, IT / Cybersecurity, Finance / Accounts, HR, Recovery / Collections, Treasury, SME / Retail / Credit, Security / Vigilance, Customer Service, MIS / Reporting, Audit / Inspection.

Also, determine if this gap affects multiple departments. If yes, list all affected departments and severity ('high', 'medium', or 'low') for each.

Return ONLY a JSON object with this exact structure:
{{
  "department": "Name of the main department from the options",
  "confidence": 0.0 to 1.0,
  "reasoning": "Detailed reasoning explaining the choice",
  "multi_department": true or false,
  "affected_departments": [
    {{"dept": "Name of department from options", "severity": "high/medium/low"}}
  ]
}}"""
    try:
        res_text = await call_gemini(prompt, json_mode=True)
        res_json = json.loads(res_text)
        
        dept_name = res_json.get("department", "Compliance")
        confidence = float(res_json.get("confidence", 0.5))
        reasoning = res_json.get("reasoning", "")
        multi_dept = bool(res_json.get("multi_department", False))
        
        affected_list = []
        raw_affected = res_json.get("affected_departments", [])
        if raw_affected:
            for item in raw_affected:
                d_name = item.get("dept")
                sev = item.get("severity", "medium")
                if d_name:
                    affected_list.append({
                        "department_id": normalize_dept_id(d_name),
                        "severity": sev.lower() if sev.lower() in ("high", "medium", "low") else "medium"
                    })
        
        main_dept_id = normalize_dept_id(dept_name)
        
        return {
            "department_id": main_dept_id,
            "confidence_score": confidence,
            "reasoning": reasoning,
            "multi_department": multi_dept,
            "affected_departments": affected_list
        }
    except Exception as e:
        logger.error(f"Classify department agent fallback: {e}")
        # Default fallback
        return {
            "department_id": "DEPT-COMPLIANCE",
            "confidence_score": 0.5,
            "reasoning": "Gemini API classification failed. Falling back to Compliance department.",
            "multi_department": False,
            "affected_departments": []
        }

async def calculate_workload_score(database: Any, employee: Dict[str, Any]) -> int:
    """
    Workload Score Formula:
    score = (open_maps * 2) + (overdue_maps * 5)
    Computed dynamically.
    """
    emp_id = employee["emp_id"]
    now = datetime.utcnow()
    
    open_maps = await database.maps.count_documents({
        "assigned_to": emp_id,
        "status": {"$in": ["assigned", "in_progress", "under_validation"]}
    })
    
    overdue_maps = await database.maps.count_documents({
        "assigned_to": emp_id,
        "status": {"$in": ["assigned", "in_progress"]},
        "deadline": {"$lt": now}
    })
    
    return (open_maps * 2) + (overdue_maps * 5)

async def select_employee_for_map(
    database: Any, 
    department_id: str, 
    gap_description: str,
    gap_tags: List[str] = None
) -> Tuple[Optional[str], str]:
    """
    Agent 3: Employee Assignment Agent.
    Input: department_id, list of employees with workload_score, expertise_tags, current_open_maps.
    Output: employee_id + assignment_reason
    """
    cursor = database.users.find({
        "department_id": department_id,
        "status": "active",
        "role": "employee"
    })
    employees = await cursor.to_list(length=100)
    
    if not employees:
        # If no employees, return None and a reason
        return None, "No active employees found in this department. Flagging for Admin triage."
        
    # Calculate scores dynamically for all candidates
    candidates_data = []
    for emp in employees:
        workload = await calculate_workload_score(database, emp)
        
        # Calculate expertise match
        expertise_match = 0
        emp_tags = [t.lower() for t in emp.get("expertise_tags", [])]
        if gap_tags:
            for tag in gap_tags:
                if tag.lower() in emp_tags:
                    expertise_match += 1
                    
        # Net score for rank logic: workload - (expertise_match * 3)
        net_score = workload - (expertise_match * 3)
        
        open_count = await database.maps.count_documents({
            "assigned_to": emp["emp_id"],
            "status": {"$in": ["assigned", "in_progress", "under_validation"]}
        })
        
        candidates_data.append({
            "emp_id": emp["emp_id"],
            "name": emp["name"],
            "workload_score": workload,
            "expertise_match": expertise_match,
            "assignment_score": net_score,
            "expertise_tags": emp.get("expertise_tags", []),
            "open_maps": open_count
        })
        
    # Sort locally to determine our fallback winner (lowest assignment score wins)
    candidates_sorted = sorted(candidates_data, key=lambda x: x["assignment_score"])
    fallback_assignee = candidates_sorted[0]["emp_id"]
    fallback_reason = f"Deterministic fallback select based on lowest assignment score of {candidates_sorted[0]['assignment_score']}."

    prompt = f"""Given this regulatory gap description and the list of active department employees with their dynamic workload score, expertise tags, and current open MAPs, select the best employee to assign the task to.

Gap Description: {gap_description}

Employees List:
{json.dumps(candidates_data, indent=2)}

Please pick the employee who is the best fit. Lower assignment scores (workload_score - expertise_match*3) indicate a better fit. Lower workload scores are better (the formula is open_MAPs*2 + overdue_MAPs*5).

Return ONLY a JSON object with this exact structure:
{{
  "employee_id": "EMP-ID-OF-CHOSEN-EMPLOYEE",
  "reasoning": "Reason why they are the best fit based on workload and expertise"
}}"""
    try:
        res_text = await call_gemini(prompt, json_mode=True)
        res_json = json.loads(res_text)
        emp_id = res_json.get("employee_id")
        reasoning = res_json.get("reasoning", "Gemini selection reasoning unavailable.")
        
        # Verify the chosen employee exists in candidates
        valid_ids = {c["emp_id"] for c in candidates_data}
        if emp_id in valid_ids:
            return emp_id, reasoning
        else:
            logger.warning(f"Gemini selected invalid employee_id {emp_id}. Falling back.")
            return fallback_assignee, fallback_reason
    except Exception as e:
        logger.error(f"Employee assignment agent fallback: {e}")
        return fallback_assignee, fallback_reason

async def check_duplicate_circular(
    database: Any,
    circular_number: str,
    title: str,
    content: str,
    date_str: str
) -> Dict[str, Any]:
    """
    Agent 4: Duplicate Circular Detection Agent.
    Input: circular detail, existing circular list.
    Output: {
      "is_duplicate": bool,
      "linked_to_circular_id": str or None,
      "relationship": 'none'/'duplicate'/'clarification'/'amendment'
    }
    """
    cursor = database.circulars.find(
        {"status": "processed"},
        {"circular_id": 1, "circular_number": 1, "title": 1, "content": 1, "date_issued": 1}
    ).limit(10) # check last 10 processed circulars
    
    existing_list = []
    async for circ in cursor:
        existing_list.append({
            "circular_id": circ["circular_id"],
            "circular_number": circ.get("circular_number", ""),
            "title": circ.get("title", ""),
            "content_snippet": circ.get("content", "")[:300],
            "date": circ.get("date_issued").isoformat() if isinstance(circ.get("date_issued"), datetime) else str(circ.get("date_issued"))
        })
        
    if not existing_list:
        return {"is_duplicate": False, "linked_to_circular_id": None, "relationship": "none"}
        
    prompt = f"""You are a regulatory auditor. Compare the incoming circular [X] with the list of existing circulars [Y] to check if it is a duplicate, clarification, or amendment of any existing circular.

Incoming Circular [X]:
Number: {circular_number}
Title: {title}
Date: {date_str}
Content excerpt: {content[:1000]}

Existing Circulars [Y]:
{json.dumps(existing_list, indent=2)}

Determine if X matches any circular in Y.
Return ONLY JSON format:
{{
  "is_duplicate": true or false,
  "linked_to_circular_id": "CIRCULAR-ID-FROM-Y" or null,
  "relationship": "duplicate/clarification/amendment/none"
}}"""
    try:
        res_text = await call_gemini(prompt, json_mode=True)
        res_json = json.loads(res_text)
        is_dup = bool(res_json.get("is_duplicate", False))
        linked_id = res_json.get("linked_to_circular_id")
        rel = res_json.get("relationship", "none")
        
        # Validate return values
        if not is_dup or not linked_id:
            return {"is_duplicate": False, "linked_to_circular_id": None, "relationship": "none"}
            
        return {
            "is_duplicate": is_dup,
            "linked_to_circular_id": linked_id,
            "relationship": rel if rel in ("duplicate", "clarification", "amendment") else "none"
        }
    except Exception as e:
        logger.error(f"Duplicate circular agent fallback: {e}")
        return {"is_duplicate": False, "linked_to_circular_id": None, "relationship": "none"}

async def generate_evidence_template(gap_type: str, gap_description: str) -> str:
    """
    Agent 5: AI-Generated Evidence Templates.
    Input: gap_type, gap_description.
    Output: markdown checklist & SOP outline.
    """
    prompt = f"""You are a bank audit manager. Given this gap type [KYC/cybersecurity/lending/etc.] and gap description, generate a structured compliance evidence template in Markdown.
Gap Type: {gap_type}
Gap Description: {gap_description}

Your template must include:
1. Checklist of required policy/system changes.
2. SOP Outline describing execution steps.
3. List of document references and proof files needed to validate resolution.

Return ONLY structured Markdown text:"""
    try:
        res_text = await call_gemini(prompt, json_mode=False)
        return res_text
    except Exception as e:
        logger.error(f"Evidence template agent fallback: {e}")
        # Return generic fallback template
        return f"""# Evidence Template: {gap_type}
*Generic Fallback Template*

### 1. Checklist of Required Changes
- [ ] Review system requirements against gap details: `{gap_description}`.
- [ ] Revise departmental SOPs and align policy documents.
- [ ] Deploy code updates or verify controls configuration in production.

### 2. SOP Outline
1. **Assessment:** Conduct technical/business review of the compliance obligation.
2. **Mitigation:** Update systems, firewall configurations, databases, or training materials.
3. **Approval:** Obtain signs-off from the security lead and department head.

### 3. Document References Needed
- Updated policy document highlighting revisions.
- Screenshots of active controls or server command-line configurations.
- Signed completion approval memo.
"""
