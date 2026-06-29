import logging

import speedtest

logger = logging.getLogger(__name__)


def get_internet_speed() -> str:
    try:
        st = speedtest.Speedtest()
        st.get_best_server()
        st.download()
        st.upload()
        r = st.results.dict()
        dl = round(r['download'] / 1_000_000, 2)
        ul = round(r['upload'] / 1_000_000, 2)
        return f"📈 Скорость: ▼ {dl} Mbps  ▲ {ul} Mbps"
    except Exception as e:
        logger.error(f"Speedtest error: {e}")
        return f"⚠️ Speedtest недоступен: {e}"
