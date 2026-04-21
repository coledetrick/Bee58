# AI Narrative Diagnosis

**Phase**: 5 (Product Differentiator)  
**Dependencies**: Phase 2 (Lambda pipeline), Phase 1 (structured rule engine with flags)

---

## Purpose

The rule engine produces structured findings (flag tokens, scores, severity). The AI layer converts those findings into plain-English narrative targeted at the nervous first-tune user — someone who understands their car, but is not a professional tuner.

**The rule engine decides. Claude interprets.**

Rules are deterministic and explainable. Claude adds tone, context, and the "what does this mean for me" layer. Claude does not override or reinterpret the rule engine's findings — it explains them.

---

## Architecture

The Analysis Lambda calls the Claude API after the rule engine finishes:

```
Rule engine findings (structured flags, scores, alerts)
    ↓
Claude API call (with retrieved context from knowledge base)
    ↓
Plain-English narrative diagnosis
    ↓
Stored in DynamoDB alongside structured findings
```

### RAG Knowledge Base

Claude's output is grounded in a corpus of real B58 tuning knowledge:
- YouTube transcript transcripts from B58/A90 tuning channels (scraper already built)
- Forum thread excerpts (known-outcome diagnostic cases)
- Tuning platform documentation

**Retrieval options** (choose one):

| Option | Complexity | Cost | Fit |
|---|---|---|---|
| AWS Bedrock Knowledge Base | Low (managed) | Higher | Good if staying all-AWS |
| OpenSearch Serverless | Medium | Higher | More control |
| Pinecone (external) | Low | Predictable pricing | Pragmatic choice |
| ChromaDB on Lambda (ephemeral) | Medium | Lowest | Fragile, not recommended |

**Recommendation for MVP**: Bedrock Knowledge Base. Fully managed, no infra to maintain, integrates with Claude API via Bedrock. Keeps the stack all-AWS for resume coherence. Revisit if cost is prohibitive.

---

## Transcript Corpus Preparation

The YouTube transcript scraper is already built. Transcripts need a cleaning pass before embedding — auto-captions garble technical terms:

| Raw caption | Should be |
|---|---|
| "H P F P" | "HPFP" |
| "W O T" | "WOT" |
| "P S I" | "PSI" |
| inconsistent units | normalized |

**Cleaning pipeline** (one-time, before embedding):
1. Normalize B58/BMW-specific acronyms (HPFP, LPFP, WGDC, IAT, KR, WOT, AFR, TDC)
2. Fix unit transcription ("twelve psi" → "12 PSI")
3. Strip filler ("um", "uh", repetitive phrases)
4. Chunk by topic boundary, not by fixed token count — a topic chunk is a coherent explanation of a single concept
5. Embed chunks → store in knowledge base with metadata (source URL, channel, topic tags)

---

## Claude API Integration

The Analysis Lambda calls Claude after running the rule engine. The prompt is built from:
1. Structured rule findings (flags, score, per-alert data)
2. Retrieved context chunks from the knowledge base
3. A system prompt that defines Claude's role and constraints

### System prompt

```
You are a helpful assistant explaining BMW B58 ECU log analysis results to the car's owner.
The owner just got their first remote tune. They are not a professional tuner — they want to know
if their car is okay, what the findings mean, and how to talk to their tuner about them.

You will be given structured diagnostic findings from a rule engine. Your job is to explain
these findings in plain English. Do not contradict or override the rule engine's findings.
Do not speculate beyond what the findings show. Be reassuring where the findings are reassuring;
be direct and clear where action is needed.

Always end with a "What to tell your tuner" section — a short, pre-written message the user
can copy and send directly.
```

### User prompt construction

```python
def build_prompt(result: DiagnosticResult, context_chunks: list[str]) -> str:
    findings_block = format_findings(result)  # structured text from flags/alerts
    context_block = "\n\n".join(context_chunks)
    
    return f"""
## Diagnostic Findings

{findings_block}

## Background Knowledge

{context_block}

## Your Task

Write a plain-English diagnosis for the car owner. Use the three-severity structure:
1. Things to address immediately
2. Things to mention at the next check-in
3. Things that are normal and expected

End with a "What to tell your tuner" section.
"""
```

### Output structure

Claude should return structured output (JSON mode or via tool use) to make frontend rendering predictable:

```json
{
  "overall_summary": "Your car looks healthy overall. One finding worth mentioning to your tuner.",
  "severity_lane": "mention",
  "findings": [
    {
      "flag": "timing_pull",
      "severity": "mention",
      "explanation": "The analyzer detected a timing pull of -4.2° on cylinder 3...",
      "action": "Mention this to your tuner at your next check-in."
    }
  ],
  "tuner_message": "Hey, ran a log today. The analyzer flagged a timing pull of -4.2° on Cyl 3. Everything else looks clean. Is this within normal range for this map?"
}
```

Use Claude's tool use / structured output feature rather than parsing freeform text.

---

## Cost and Latency

- Bedrock Knowledge Base retrieval adds ~200-500ms
- Claude API call adds ~1-3 seconds depending on output length
- Total Analysis Lambda duration rises from ~5s to ~8-10s
- This is fine for the async polling architecture — the user is already waiting

**Cost estimate (rough)**: At low MVP traffic (~100 analyses/day), Claude API costs are negligible. Model choice matters at scale — use Claude Haiku 4.5 for the narrative (cheaper, fast, sufficient for this task). Reserve Sonnet for evaluation and prompt development.

---

## Grounding and Accuracy

Claude should not invent diagnostic findings. The rule engine's output is the ground truth. Prompt engineering constraints:
- "Do not introduce findings not present in the structured data above"
- "If the findings show no issues, say so clearly and warmly — do not invent reassurance that contradicts the data"
- Validate Claude's output: if it references a flag not in `result.flags`, log a warning and fall back to a template-based narrative

The fallback path (if Claude call fails or returns malformed output) is a template-rendered narrative from the structured findings — simpler, but always correct. Claude enhances; the template is the floor.

---

## Data Privacy Note

Log files contain ECU sensor data — no PII. However, logs may be associated with a user's car (VIN is not in the log, but tune stage and platform are). For the anonymous MVP, this is low risk. If user accounts are added (Phase 5+), revisit data handling and add opt-in consent for logs used in AI training.
