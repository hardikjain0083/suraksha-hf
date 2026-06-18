import re
from typing import List, Dict


class RBIParser:
    """Specialized parser for RBI regulatory document types."""

    @staticmethod
    def parse_master_direction(text: str) -> Dict:
        """Parse RBI Master Directions (comprehensive multi-chapter regulatory docs)."""
        sections = re.split(r"\n(?=CHAPTER\s+\d+|\d+\.\s+[A-Z])", text)
        clauses = []

        for section in sections:
            if len(section.strip()) < 100:
                continue

            chapter_match = re.match(r"CHAPTER\s+(\d+)", section, re.IGNORECASE)
            section_match = re.match(r"(\d+)\.\s+", section)

            prefix = ""
            if chapter_match:
                prefix = f"Chapter {chapter_match.group(1)}"
            elif section_match:
                prefix = section_match.group(1)

            for obl_text, obl_type in re.findall(
                r"([^.]*?\b(shall|must|should|may)\b[^.]*\.)", section, re.IGNORECASE
            ):
                clauses.append(
                    {
                        "clause_number": prefix or None,
                        "text": obl_text.strip(),
                        "obligation_type": obl_type.lower(),
                        "severity": (
                            "critical" if obl_type.lower() in ("shall", "must") else "medium"
                        ),
                        "source": "master_direction",
                    }
                )

        return {
            "document_type": "master_direction",
            "clauses": clauses,
            "chapter_count": len(
                [s for s in sections if re.match(r"CHAPTER\s+\d+", s, re.IGNORECASE)]
            ),
        }

    @staticmethod
    def parse_circular(text: str) -> Dict:
        """Parse a standard RBI Circular (numbered paragraphs)."""
        paragraphs = re.split(r"\n(?=\d+\.\d+\s+|\(\d+\)\s+)", text)
        clauses = []

        for para in paragraphs:
            para = para.strip()
            if len(para) < 50:
                continue

            ref_match = re.search(
                r"(DBOD\.|DBS\.|DPSS\.|DNBS\.|FIDD\.)\s*No\.?\s*[\w./]+", para
            )
            ref = ref_match.group(0) if ref_match else None

            for obl_type in ("shall", "must", "should", "may"):
                if re.search(rf"\b{obl_type}\b", para, re.IGNORECASE):
                    clauses.append(
                        {
                            "clause_number": None,
                            "text": para[:500],
                            "obligation_type": obl_type,
                            "severity": (
                                "critical" if obl_type in ("shall", "must") else "medium"
                            ),
                            "penalty_reference": ref,
                            "source": "circular",
                        }
                    )
                    break

        return {"document_type": "circular", "clauses": clauses}

    @staticmethod
    def parse_notification(text: str) -> Dict:
        """Parse RBI Notifications (often amendments to existing regulations)."""
        amendments = re.findall(
            r"(?:amend|modify|substitute|replace).*?(?:circular|direction|notification)"
            r".*?(?:No\.?\s*[\w./]+)",
            text,
            re.IGNORECASE,
        )
        return {
            "document_type": "notification",
            "amendments": amendments,
            "clauses": [],
        }

    @staticmethod
    def detect_and_parse(text: str, tables: List = None) -> Dict:
        """Auto-detect document type and dispatch to the correct parser."""
        text_upper = text.upper()

        if "MASTER DIRECTION" in text_upper:
            return RBIParser.parse_master_direction(text)
        elif "NOTIFICATION" in text_upper and "AMEND" in text_upper:
            return RBIParser.parse_notification(text)
        else:
            return RBIParser.parse_circular(text)
