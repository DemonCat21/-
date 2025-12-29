# predictions.py
import os
import random
import logging
import asyncio
from typing import List
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[2]
PREDICTIONS_DIR = BASE_DIR / "data" / "predictions"

async def load_predictions() -> List[str]:
    """Завантажує передбачення без блокування event loop."""
    PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)

    async def _ensure_dir_and_seed() -> List[str]:
        if os.path.exists(PREDICTIONS_DIR):
            return []
        logger.warning(f"Директорія '{PREDICTIONS_DIR}' не знайдена. Створюю її.")
        os.makedirs(PREDICTIONS_DIR, exist_ok=True)
        example_text = (
            "Сьогодні на вас чекає великий успіх у всіх починаннях!\n"
            "Несподівана зустріч принесе відповіді на важливі питання.\n"
            "Ваша енергія сьогодні на піку. Використайте її з розумом."
        )

        def _write_seed():
            with open(PREDICTIONS_DIR / "all_predictions.txt", "w", encoding="utf-8") as f:
                f.write(example_text)

        await asyncio.to_thread(_write_seed)
        return [line.strip() for line in example_text.split("\n") if line.strip()]

    seeded = await _ensure_dir_and_seed()
    if seeded:
        return seeded

    async def _load_all() -> List[str]:
        predictions: List[str] = []

        def _read_one(path: Path) -> List[str]:
            try:
                with path.open("r", encoding="utf-8") as f:
                    return [ln.strip() for ln in f.readlines() if ln.strip()]
            except Exception:
                return []

        try:
            for filename in os.listdir(PREDICTIONS_DIR):
                if filename.endswith(".txt"):
                    path = PREDICTIONS_DIR / filename
                    predictions.extend(await asyncio.to_thread(_read_one, path))
        except Exception as e:
            logger.error(f"Помилка під час завантаження передбачень: {e}", exc_info=True)

        return predictions

    predictions = await _load_all()

    if not predictions:
        logger.warning("Не знайдено файлів з передбаченнями у директорії 'predictions'.")
        return ["На жаль, зірки сьогодні мовчать. Спробуйте завтра."]

    return predictions


async def get_random_prediction() -> str:
    """Повертає випадкове передбачення асинхронно."""
    predictions = await load_predictions()
    return random.choice(predictions)
