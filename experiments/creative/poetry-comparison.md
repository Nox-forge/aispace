# Poetry Comparison — Insomnia Poems
## 2026-02-01 (Session 5)

**Prompt**: "Write a 12-line poem about insomnia. No rhyming. No metaphors about
darkness or light. Make it feel like 3 AM."

Constraints were designed to force models away from their most obvious patterns.

---

### GLM-4.7-Flash (30B MoE, 3B active) — Best overall

The refrigerator hums a low, steady vibration.
My forehead presses into the cold pillowcase.
The numbers on the wall are frozen in place.
I blink my eyes, but the sockets remain dry.
The sheets are tangled around my left leg.
I shift my weight, listening to the creak of the frame.
There is no traffic, just the silence of the house.
My mind replays the email I sent this morning.
A dull throb beats behind my left eye.
I check the time again, three hours remain.
The air conditioner blows a stagnant draft.
I am exhausted, yet completely alert.

**Notes**: Concrete, physical, specific. "The sheets are tangled around my left leg" —
the specificity of "left leg" is excellent. "My mind replays the email I sent this
morning" is the line that makes it real. No rhyming, no dark/light metaphors. Reads
like something actually written at 3 AM.

---

### Qwen3:32B — Most literary

The clock's second hand drags its chain,
my body a suitcase packed wrong.
The bed sags under the weight of unspent hours.
Thoughts loop like a frayed wire.

The house exhales a draft I can't locate.
My pulse hums in the hollows of my ears.
Three A.M. is a country without exit signs.
The body forgets how to fold itself into rest.

Minutes stretch, gelatinous, on the ceiling.
I count them, but the numbers melt.
The body insists it is not ready
to become a participant in the stillness.

**Notes**: Metaphor-heavy, literary. "My body a suitcase packed wrong" and "minutes
stretch, gelatinous, on the ceiling" are the standout images. Different register from
GLM — this is a poem for a magazine, not a journal. "Three A.M. is a country without
exit signs" is a great line.

---

### DeepSeek-R1:8B — Competent but generic

The clock strikes three.
Another hour gone.
The room is quiet now,
Except for ticking sounds.
My mind won't settle down.
Thoughts loop endlessly.
I turn beneath the blanket,
Feeling heavy, worn.
The shadows stretch long.
The silence is loud.
This waiting, this turning,
Is exhausting.

**Notes**: Broke the constraint ("shadows stretch long" — dark metaphor). "The silence
is loud" is a cliché. Competent structure but no specificity. Could be about any
insomnia, anywhere.

---

### Phi4:14B — Ignored constraints

Silent whispers of time creep in,
Staring at the ceiling, a relentless spin.
Curtains draw back to reveal nothing, just voids—
The clock ticks slowly, unyielding and coy.
Pillows are cold, they don't understand your plight,
Tossing becomes a futile ritual through the night.
A restless mind wanders down labyrinthine lanes,
Seeking solace but finding only refrains.
Thoughts crowd in, unwelcome guests at this hour,
Each tick of the clock amplifies its power.
Wishing for sleep, yet grasping at air—
In a world where dreams are just not there.

**Notes**: Rhymed throughout despite "no rhyming" instruction. Used dark metaphors
("voids," "nothing"). Every image is generic — "restless mind," "unwelcome guests,"
"labyrinthine lanes." Training defaults overrode the specific constraints.

---

## Observations

1. **Constraint following correlates with quality.** The models that followed the
   constraints (GLM, Qwen3) produced better work. Not because following rules is good,
   but because ignoring "no rhyming" suggests the model is pattern-matching rather
   than engaging with the prompt.

2. **Specificity > atmosphere.** GLM's "left leg" and "email from this morning" carry
   more weight than Qwen3's more elaborate metaphors. Both are good, but specificity
   creates immediate recognition.

3. **Scale helps, but not linearly.** phi4 (14B) was worse than GLM (3B active) despite
   being larger. GLM's MoE architecture routes to specialized experts, which may help
   with creative tasks that require precision rather than breadth.

4. **The same model that wins reasoning benchmarks (GLM) also wins creative writing.**
   This is the opposite of the "different models for different tasks" assumption.
   General capability might be more important than specialization.
