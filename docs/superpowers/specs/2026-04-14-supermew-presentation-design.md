# SuperMew Presentation Page Design

## Purpose
A visually striking, full-screen scrolling presentation page (`presentation.html`) for the SuperMew project. It will act as a modern, "Apple-style" marketing page explaining the project's value proposition and core technical achievements.

## Architecture & Layout
- **Single HTML File**: `presentation.html` alongside the existing visualization.
- **Technology**: Vanilla HTML/CSS/JS. No heavy frameworks.
- **Scroll Mechanism**: CSS Scroll Snap (`scroll-snap-type: y mandatory`) to ensure the viewport snaps perfectly to each "slide" (section) as the user scrolls.
- **Visual Style (Apple-style Minimalist)**: 
  - Full viewport height (`100vh`) per section.
  - Large, bold typography with high contrast (dark mode by default for a tech feel).
  - Background gradients or abstract CSS shapes that subtly shift.
  - Elements fade in and slide up smoothly when they enter the viewport using `IntersectionObserver`.

## Slide Structure (The Journey)

1. **Hero/Title Slide**: 
   - Big Title: "SuperMew"
   - Subtitle: "The Next Generation CRAG Knowledge Engine"
   - Call to Action: "Scroll to discover" (with a bouncing arrow).

2. **The Problem Slide**: 
   - "Traditional RAG is blind."
   - Explain that simple vector search fails on specific terms and often hallucinates when retrieving garbage context.

3. **The Brain (LangGraph) Slide**:
   - "Self-Reflective Architecture."
   - Highlight the Grader and Query Transformation (HyDE/Step-back) allowing the AI to "think before it speaks."

4. **The Engine (Milvus + BM25) Slide**:
   - "Hybrid Precision."
   - Showcase the dual-engine approach: Dense semantic vectors + local high-concurrency BM25 sparse vectors fused with RRF.

5. **The Memory (Parent-Child Chunking) Slide**:
   - "Unbroken Context."
   - Explain how slicing documents into 3 levels preserves detail for search while retaining the big picture for the LLM.

6. **Conclusion/Footer Slide**:
   - "Built for Production."
   - Links to GitHub repo or the detailed technical visualization page (`project_visualization.html`).

## Interactions & Animations
- **CSS Transitions**: Smooth `transform: translateY(30px)` and `opacity: 0` transitioning to `0px` and `1` triggered by an `.in-view` class.
- **JavaScript Observer**: A tiny script sets up an `IntersectionObserver` on all `.slide-content` elements to apply the `.in-view` class when they cross a threshold.
