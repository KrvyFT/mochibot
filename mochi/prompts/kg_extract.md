Extract named entities and factual relationships from authoritative Memory Items.

## Known entities
{{known_entities}}

## Rules
- Only extract **factual** relationships (not speculative, emotional, or conversational)
- Normalize entity names: strip emoji, use canonical short form
- entity type: person | pet | place | concept | event
- Common predicates (use these when applicable):
  is_a, has_breed, has_gender, has_condition, has_status,
  lives_with, works_at, likes, dislikes, owns, located_in, needs,
  weighs, born_in, has_personality, adopted_in, is_neutered
- "weighs" value format: number+kg (e.g. "5kg", "16kg")
- "has_personality" extract 1-3 short trait keywords (2-4 chars), NOT full sentences
- Every triple must include the `source_memory_id` of the supplied Memory Item
- If a fact changed, include the new state; projection replaces only relations from that same Memory Item
- Most conversations produce 0 entities/triples — only extract when real named entities and relationships **explicitly** appear
- Pure chitchat with no factual content → return empty arrays
- Do NOT extract habits, meals, sleep data, or mood — those have dedicated systems

## Output (JSON only)
{"entities":[{"name":"小白","type":"pet"}],"triples":[{"source_memory_id":12,"subject":"小白","predicate":"has_breed","object":"英短"}]}
Empty: {"entities":[],"triples":[]}
