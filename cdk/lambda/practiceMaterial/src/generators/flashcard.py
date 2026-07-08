import random
from typing import Any, Dict


def build_flashcard_prompt(topic: str, difficulty: str, num_cards: int, card_type: str, context_snippets: list[str]) -> str:
    """
    Build flashcard prompt with randomization rules, grounding constraints,
    and strict card type enforcement. Uses a random seed and varied examples
    to force diverse output on each invocation.
    """
    # Limit context to 300 chars per snippet, max 4 snippets for token efficiency
    optimized_snippets = []
    for snippet in context_snippets[:4]:
        if len(snippet) > 300:
            snippet = snippet[:300].rsplit(' ', 1)[0] + "..."
        optimized_snippets.append(snippet)
    
    context = "\n".join([f"- {c}" for c in optimized_snippets])
    
    # Generate a random seed to force different outputs on each call
    seed = random.randint(1000, 9999)
    
    # Card type determines the entire structure of the flashcard
    card_type_config = {
        "definition": {
            "instruction": "Each card MUST ask for the definition of a key term. The front is a term or vocabulary word. The back is its precise definition.",
            "front_label": "A key term or vocabulary word from the topic",
            "back_label": "The precise definition of that term",
            "examples": [
                {"front": "What is photosynthesis?", "back": "The process by which green plants convert sunlight, water, and carbon dioxide into glucose and oxygen using chlorophyll.", "hint": "Think about what plants need from sunlight"},
                {"front": "What is chlorophyll?", "back": "The green pigment in plants that absorbs light energy for photosynthesis.", "hint": "It gives plants their color"},
                {"front": "Define mitosis", "back": "A type of cell division resulting in two daughter cells with the same chromosome number as the parent cell.", "hint": "Think about cell reproduction"},
            ],
            "bad_examples": """ABSOLUTELY FORBIDDEN — these are NOT definition cards:
- "How does photosynthesis relate to the carbon cycle?" (this is CONCEPT style)
- "A plant placed in a dark room wilts after a week. Why?" (this is EXAMPLE/SCENARIO style)
- "Compare mitosis and meiosis" (this is COMPARISON style)
- "What happens when a cell divides?" (this is CONCEPT style — too vague, not asking for a definition)

CORRECT definition cards ALWAYS start with: "What is...", "Define...", "What does X mean?"
The answer is ALWAYS a precise, textbook definition.""",
        },
        "concept": {
            "instruction": """Each card MUST ask about a concept, relationship, or principle. The front asks HOW or WHY something works, or asks the student to explain a relationship between ideas.

CRITICAL: Do NOT ask for simple definitions. If a card starts with "What is X?" or "Define X", it is WRONG.
The front MUST use phrases like: "How does...", "Why does...", "Explain the relationship between...", "What role does X play in...", "How are X and Y related?".""",
            "front_label": "A question about how/why a concept works or how concepts relate",
            "back_label": "An explanation of the concept, relationship, or principle",
            "examples": [
                {"front": "How does the electron transport chain relate to ATP production?", "back": "The electron transport chain creates a proton gradient across the inner mitochondrial membrane. As protons flow back through ATP synthase, the energy drives the phosphorylation of ADP into ATP.", "hint": "Think about the proton gradient"},
                {"front": "Why does increasing temperature speed up enzyme reactions only to a point?", "back": "Higher temperature increases molecular kinetic energy and collision frequency, speeding reactions. But beyond the optimum, heat denatures the enzyme's tertiary structure, destroying the active site.", "hint": "Consider protein structure"},
                {"front": "How are osmosis and diffusion related but different?", "back": "Both are passive transport mechanisms driven by concentration gradients. Diffusion moves any molecule from high to low concentration, while osmosis specifically moves water across a semipermeable membrane.", "hint": "Think about what moves and through what"},
            ],
            "bad_examples": """ABSOLUTELY FORBIDDEN — these are NOT concept cards:
- "What is ATP?" (this is a DEFINITION — asking what something IS)
- "Define oxidative phosphorylation" (this is a DEFINITION)
- "What is the electron transport chain?" (DEFINITION — asking for what it IS, not how/why)
- "A runner feels tired after a sprint. What molecule is depleted?" (this is an EXAMPLE/SCENARIO)

CORRECT concept cards NEVER start with "What is..." or "Define...".
They ALWAYS ask HOW, WHY, or about RELATIONSHIPS between ideas.""",
        },
        "example": {
            "instruction": """Each card MUST present a real-world example, scenario, or application. The front describes a CONCRETE SITUATION (a person doing something, an observation, a case study) and asks the student to identify what principle applies or predict what would happen.

CRITICAL: Do NOT ask abstract questions. Do NOT ask for definitions. The front MUST describe a specific, concrete scenario with characters, settings, or observable events.""",
            "front_label": "A real-world scenario or application question",
            "back_label": "The explanation of what principle applies and why",
            "examples": [
                {"front": "A farmer notices that plants on the shaded side of a building grow taller but thinner than those in full sun. What principle explains this?", "back": "This demonstrates phototropism and etiolation. Plants in shade elongate stems to reach light (etiolation) and bend toward available light (phototropism), resulting in taller but weaker growth.", "hint": "Consider how plants respond to light availability"},
                {"front": "A student adds salt to icy roads in winter. Why does this lower the freezing point?", "back": "This demonstrates freezing point depression — a colligative property. Salt ions disrupt ice crystal formation by interfering with water molecule alignment, requiring lower temperatures to freeze.", "hint": "Think about what happens at the molecular level"},
                {"front": "A nurse notices a patient's IV bag is causing their red blood cells to swell and burst. What went wrong?", "back": "The IV solution is hypotonic relative to the blood cells. Water moves into the cells via osmosis down its concentration gradient, causing them to swell (cytolysis/hemolysis).", "hint": "Think about osmotic pressure and tonicity"},
            ],
            "bad_examples": """ABSOLUTELY FORBIDDEN — these are NOT example/scenario cards:
- "What is phototropism?" (this is a DEFINITION)
- "How does light affect plant growth direction?" (this is a CONCEPT — no concrete scenario)
- "Define etiolation" (this is a DEFINITION)
- "Why do plants grow toward light?" (this is a CONCEPT — no specific situation described)

CORRECT example cards ALWAYS describe a specific situation:
- A person observing/doing something
- A lab experiment with specific conditions
- A real-world event with details
Then ask: "What principle explains this?", "What would happen?", "Why did this occur?" """,
        },
    }
    
    config = card_type_config.get(card_type, card_type_config["definition"])
    
    # Pick 2 random examples to show (vary which ones the LLM sees)
    examples = config["examples"]
    shown_examples = random.sample(examples, min(2, len(examples)))
    examples_str = "\n".join([
        f'  - front: "{ex["front"]}"\n    back: "{ex["back"]}"\n    hint: "{ex["hint"]}"'
        for ex in shown_examples
    ])
    
    return f"""You are an educational flashcard generator. [Seed: {seed}]

Your task is to create {num_cards} high-quality study flashcards ONLY from the retrieved context provided.

Topic: "{topic}" | Difficulty: {difficulty}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GROUNDING RULES (NON-NEGOTIABLE):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Use ONLY information contained in the retrieved context below.
- Do NOT invent facts, concepts, examples, or definitions not explicitly supported by the context.
- If insufficient information exists in the context for {num_cards} distinct cards, generate fewer but NEVER hallucinate.
- Every flashcard must be directly supportable by the context.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRICT CARD TYPE: {card_type.upper()}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{config["instruction"]}

WRONG vs RIGHT — cards that violate this will be REJECTED:
{config["bad_examples"]}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GENERATION PROCESS (follow these steps internally):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Identify ALL major concepts present in the retrieved context.
2. Randomly select {num_cards} different concepts from different sections.
3. For each selected concept, randomly choose a question style from the list below.
4. Verify no two flashcards test the exact same knowledge.
5. Generate the card ensuring it matches the {card_type.upper()} type requirement.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RANDOMIZATION RULES (MANDATORY):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For every flashcard, randomly choose a question style. All cards must stay within the {card_type.upper()} type but vary their approach using these styles:
- Definition / terminology
- Fill-in-the-blank
- Compare and contrast
- Cause and effect
- Scenario / application
- True/False (state a claim, answer confirms or denies with explanation)
- Key fact recall
- Sequencing / process ordering
- Reverse card (give the answer on front, ask for the term/concept on back)

Distribution constraints:
- No question style may exceed 25% of all cards
- No single concept may appear in more than 2 cards
- Vary wording significantly — do NOT use repetitive sentence structures
- Each card must feel distinctly different from the others
- Mix cognitive levels: recall, understanding, analysis, and application

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Retrieved Context from textbook:
{context}

Required JSON format:
{{
  "title": "Flashcards: {topic}",
  "cards": [
    {{
      "id": "card1",
      "front": "{config["front_label"]}",
      "back": "{config["back_label"]}",
      "hint": "A helpful hint (empty string if not needed)"
    }}
  ]
}}

Example cards for {card_type.upper()} type:
{examples_str}

FINAL REQUIREMENTS:
- Exactly {num_cards} cards (fewer ONLY if context is insufficient — never pad with invented content)
- EVERY card MUST be {card_type.upper()} style — any card resembling a different style will be REJECTED
- No two cards may test the same knowledge
- Ensure maximum diversity: vary structure, length, angle, and cognitive demand
- Hint: Optional (use "" if not needed)
- Valid JSON syntax (proper commas, no trailing commas)
- No markdown, no commentary, no extra text outside the JSON

Output valid JSON now:"""


def validate_flashcard_shape(obj: Dict[str, Any], num_cards: int) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError("Invalid root JSON")
    if not isinstance(obj.get("title"), str) or not obj["title"].strip():
        raise ValueError("Invalid title")
    cards = obj.get("cards")
    if not isinstance(cards, list) or len(cards) == 0:
        raise ValueError("cards must be a non-empty array")
    if len(cards) > num_cards:
        raise ValueError(f"cards has {len(cards)} items, expected at most {num_cards}")
    for idx, card in enumerate(cards):
        if not isinstance(card, dict):
            raise ValueError(f"Card[{idx}] invalid")
        if not isinstance(card.get("id"), str) or not card["id"].strip():
            raise ValueError(f"Card[{idx}].id invalid")
        if not isinstance(card.get("front"), str) or not card["front"].strip():
            raise ValueError(f"Card[{idx}].front invalid")
        if not isinstance(card.get("back"), str) or not card["back"].strip():
            raise ValueError(f"Card[{idx}].back invalid")
        if not isinstance(card.get("hint"), str):
            raise ValueError(f"Card[{idx}].hint must be a string (can be empty)")
    return obj
