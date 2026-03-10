import random
import time
import requests
import json
import logging
import threading
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from random import randint, uniform
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementClickInterceptedException,
    StaleElementReferenceException,
    WebDriverException,
    MoveTargetOutOfBoundsException
)
from selenium.webdriver.common.keys import Keys

# --- Настройка логгирования ---
logging.basicConfig(filename='warpcast.log', level=logging.INFO,
                    format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s')

# --- Глобальные блокировки для файлов ---
text_file_lock = threading.Lock()
comment_file_lock = threading.Lock()
picture_folder_lock = threading.Lock()

try:
    with open("config.json", "r", encoding="utf-8") as f:
        _config_temp = json.load(f)
    PAUSE_MIN, PAUSE_MAX = _config_temp.get("pause_range", [2, 5])

except Exception as e:
    logging.warning(f"Не удалось предварительно загрузить паузы из config.json: {e}. Используются значения по умолчанию [2, 5].")
    PAUSE_MIN, PAUSE_MAX = 2, 5



def load_ads_ids(filename="ADSid.txt"):
    """Загружает список ID профилей из текстового файла."""
    try:
        filepath = Path(filename)
        if not filepath.is_file():
            logging.error(f"Файл с ID профилей '{filename}' не найден.")
            return []
        with open(filepath, "r", encoding="utf-8") as f:
            # Читаем строки, убираем пробелы по краям, фильтруем пустые строки
            ids = [line.strip() for line in f if line.strip()]
        if not ids:
            logging.error(f"Файл '{filename}' пуст или не содержит валидных ID.")
            return []
        logging.info(f"Загружено {len(ids)} ID профилей из файла '{filename}'.")
        return ids
    except Exception as e:
        logging.error(f"Ошибка при чтении файла ID '{filename}': {e}", exc_info=True)
        return []


# --- Генерация текста через OpenRouter ---
def generate_text_openrouter(channel_name, api_key, model, system_prompt):
    """Генерирует текст для канала с использованием OpenRouter API."""
    if not api_key:
        logging.error(f"Пропуск генерации для '{channel_name}': API ключ OpenRouter не предоставлен.")
        return None
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Напиши описание для канала {channel_name}"}
            ]
        }
        # Добавлен таймаут для запроса
        response = requests.post(url, headers=headers, json=data, timeout=45)
        response.raise_for_status() # Проверка на HTTP ошибки (4xx, 5xx)

        result = response.json()
        logging.info(f"Ответ от OpenRouter для '{channel_name}' получен.")

        # Более безопасное извлечение результата
        content = result.get('choices', [{}])[0].get('message', {}).get('content')
        if content:
            logging.info(f"Сгенерирован текст: '{content[:50]}...'")
            return content
        else:
            logging.warning(f"Не удалось извлечь текст из ответа OpenRouter. Ответ: {result}")
            return None

    # Ловим конкретные ошибки requests
    except requests.exceptions.Timeout:
        logging.error(f"Ошибка OpenRouter API: Таймаут запроса для канала '{channel_name}'.")
    except requests.exceptions.HTTPError as http_err:
        logging.error(f"Ошибка OpenRouter API: HTTP ошибка {http_err.response.status_code} для '{channel_name}'. Ответ: {http_err.response.text}")
    except requests.exceptions.RequestException as req_err:
        logging.error(f"Ошибка OpenRouter API: Проблема с запросом для '{channel_name}': {req_err}")
    # Ошибка парсинга или структуры ответа
    except (KeyError, IndexError, TypeError) as parse_err:
        logging.error(f"Ошибка парсинга ответа от OpenRouter для '{channel_name}': {parse_err}")
        if 'response' in locals(): # Проверяем, была ли переменная response создана
             logging.error(f"Ответ сервера: {response.text}")
    # Любая другая неожиданная ошибка
    except Exception as e:
        logging.error(f"Неожиданная ошибка в generate_text_openrouter для '{channel_name}': {type(e).__name__} - {e}", exc_info=True)
    return None


# --- Утилиты ---
def get_random_line_and_remove(path, lock):
    """
    Читает случайную строку из файла, удаляет ее и перезаписывает файл.
    Использует блокировку для потокобезопасности.
    ВАЖНО: Неэффективно для очень больших файлов!
    """
    # Блокируем доступ к файлу для других потоков
    with lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if not lines:
                # Используем warning, т.к. ValueError прервет выполнение потока
                logging.warning(f"Файл {path} пустой или не содержит строк.")
                return None # Возвращаем None вместо ошибки

            line = random.choice(lines).strip()

            # Аккуратное удаление строки (учитывая возможное отсутствие \n в конце файла)
            lines_to_remove = [l for l in lines if l.strip() == line]
            if lines_to_remove:
                 # Удаляем только одно вхождение, если их несколько
                lines.remove(lines_to_remove[0])
            else:
                # Если строка без \n была последней, она могла не найтись выше
                logging.warning(f"Не удалось найти строку '{line}' для удаления в {path} (возможно, уже удалена другим потоком?)")
                # Можно просто вернуть строку без удаления или вернуть None
                return line # Пока возвращаем как есть

            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            logging.info(f"Взята и удалена строка из {path}: '{line[:30]}...'")
            return line
        except FileNotFoundError:
            logging.error(f"Ошибка в get_random_line_and_remove: Файл {path} не найден.")
            return None
        except Exception as e:
            # Логгируем более подробную ошибку
            logging.error(f"Ошибка в get_random_line_and_remove ({path}): {type(e).__name__} - {e}", exc_info=True)
            return None

def get_random_picture(folder_path, lock):  # Убрали _and_remove
    """Выбирает случайную картинку из папки, НЕ УДАЛЯЯ ЕЕ."""
    with lock:
        try:
            # Преобразуем folder_path в абсолютный путь, если он относительный
            base_folder = Path(folder_path).resolve()  # .resolve() делает путь абсолютным

            if not base_folder.is_dir():
                logging.error(f"Папка с картинками '{base_folder}' не найдена или не является директорией.")
                return None

            image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
            # Собираем АБСОЛЮТНЫЕ пути к файлам
            image_files = [str(f_path.resolve()) for f_path in base_folder.iterdir() if
                           f_path.is_file() and f_path.suffix.lower() in image_extensions]

            if not image_files:
                logging.warning(f"В папке {base_folder} не найдено картинок с расширениями: {image_extensions}.")
                return None

            chosen_image_path = random.choice(image_files)
            logging.info(f"Выбрана картинка для поста (абсолютный путь): {chosen_image_path}")
            return chosen_image_path  # Возвращаем уже абсолютный путь

        except Exception as e:
            logging.error(f"Ошибка при выборе картинки из {folder_path}: {e}", exc_info=True)
            return None
def remove_file_if_exists(file_path_str, lock):
    """Безопасно удаляет файл, если он существует, используя блокировку."""
    if not file_path_str:
        return
    with lock:
        try:
            file_path = Path(file_path_str)
            if file_path.is_file():
                file_path.unlink()
                logging.info(f"Файл {file_path_str} успешно удален.")
            else:
                logging.warning(f"Файл {file_path_str} для удаления не найден.")
        except Exception as e:
            logging.error(f"Ошибка при удалении файла {file_path_str}: {e}", exc_info=True)

def scroll_page(driver, duration=10):
    """Скроллит страницу вверх-вниз в течение duration секунд."""
    end_time = time.time() + duration
    while time.time() < end_time:
        scroll_distance = random.randint(200, 800)
        try:
            # Вверх
            for _ in range(random.randint(1, 4)):
                driver.execute_script(f"window.scrollBy({{'top': {scroll_distance}, 'behavior': 'smooth'}});")
                time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))
            # Вниз
            driver.execute_script(f"window.scrollBy({{'top': {-scroll_distance}, 'behavior': 'smooth'}});")
            time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))
        except WebDriverException as e:
            logging.warning(f"Ошибка при скроллинге страницы: {e}")
            break # Прерываем скролл, если возникла ошибка WebDriver

def delete_post(driver):
    logging.info("Переход в профиль")
    try:
        driver.get("https://warpcast.com/")
        profile_button = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable(
                (By.XPATH, "//div[contains(@class, 'cursor-pointer') and .//div[text()='Profile']]"))
        )
        profile_button.click()
        logging.info("Клик по кнопке 'Profile'.")
        time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))

        logging.info("--- Начало процесса удаления постов ---")

        post_in_profile_list_xpath = "//div[contains(@class, 'cursor-pointer') and contains(@class, 'px-4') and contains(@class, 'py-2') and .//div[contains(@class, 'line-clamp-feed')]]"

        # XPath для выпадающего меню (для ожидания его появления)
        dropdown_menu_xpath = "//div[@data-radix-menu-content and @data-state='open' and @role='menu']"

        # XPath для опции "Delete cast" в выпадающем меню
        delete_cast_option_xpath = "//div[@data-radix-menu-content and @data-state='open' and @role='menu']//div[@role='menuitem' and contains(., 'Delete cast')]"

        # XPath для уникального элемента на странице профиля (например, кнопка "Edit Profile")
        profile_page_indicator_xpath = "//button[contains(text(),'Edit Profile')]"

        deleted_posts_count = 0
        post_processing_attempts = 0
        max_post_processing_attempts = 30  # Ограничение, чтобы избежать бесконечного цикла

        while post_processing_attempts < max_post_processing_attempts:
            logging.info(
                f"Попытка {post_processing_attempts + 1}/{max_post_processing_attempts}. Поиск постов на странице профиля...")
            time.sleep(uniform(1.5, 2.5))

            posts_on_profile_page = driver.find_elements(By.XPATH, post_in_profile_list_xpath)

            num_found_posts = len(posts_on_profile_page)
            logging.info(f"Найдено {num_found_posts} постов на текущем экране.")

            if num_found_posts <= 2:
                if num_found_posts > 0:
                    logging.info(
                        f"Осталось {num_found_posts} поста(ов). Оставляем последние 2 поста. Завершение удаления.")
                else:
                    logging.info("Больше постов для удаления на странице профиля не найдено (список пуст).")
                break



            logging.info(f"Продолжаем удаление. Сейчас найдено {num_found_posts} постов (будет обработан первый).")

            current_post_element = posts_on_profile_page[0]
            post_text_for_log = "не удалось извлечь текст"
            try:
                text_element = current_post_element.find_element(By.XPATH,
                                                                 ".//div[contains(@class, 'line-clamp-feed')]")
                post_text_for_log = text_element.text[:50].replace("\n", " ") + "..."
            except:
                pass
            logging.info(f"Обработка поста: '{post_text_for_log}'")

            # Запоминаем URL текущей страницы профиля для возможного принудительного возврата
            original_profile_url = driver.current_url

            try:
                # --- ШАГ 1: Клик по посту для перехода  ---
                logging.debug("Попытка перехода на страницу поста через клик по времени...")
                post_clicked_for_navigation = False

                # Сначала прокручиваем всю карточку поста в видимую область (оставляем как было)
                driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
                                      current_post_element)
                time.sleep(uniform(0.3, 0.7))  # Пауза после общего скролла

                # Сначала получим имя пользователя из поста, если возможно
                post_author_username = "unknown_author"
                try:
                    author_link_element = current_post_element.find_element(By.XPATH,
                                                                            ".//a[contains(@class, 'font-semibold')]")
                    post_author_username = author_link_element.text.strip()
                    if not post_author_username:
                        href_value = author_link_element.get_attribute('href')
                        if href_value and '/' in href_value:
                            post_author_username = href_value.split('/')[-1]
                    logging.debug(f"Предполагаемый автор поста: {post_author_username}")
                except NoSuchElementException:
                    logging.warning("Не удалось определить автора поста для уточнения XPath времени.")

                # XPath для времени с проверкой автора
                time_link_xpath_specific = (
                    f".//a[starts-with(@href, '/{post_author_username}/') and "
                    f"descendant::div[contains(@class, 'text-faint') and "
                    f"string-length(normalize-space(text())) > 0 and string-length(normalize-space(text())) < 6]]"
                )
                # Общий XPath для времени
                time_link_xpath_general = ".//a[descendant::div[contains(@class, 'text-faint') and string-length(normalize-space(text())) > 0 and string-length(normalize-space(text())) < 6]]"

                time_link_element_to_click = None

                # Попытка 1: Найти ссылку времени с автором
                try:
                    logging.debug(f"Попытка найти ссылку на время по XPath (с автором): {time_link_xpath_specific}")
                    time_link_element_candidate = current_post_element.find_element(By.XPATH, time_link_xpath_specific)

                    logging.info(
                        "Найдена предполагаемая ссылка на время (с автором). Прокрутка и ожидание кликабельности...")
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
                                          time_link_element_candidate)
                    time.sleep(uniform(0.5, 1.0))

                    time_link_element_to_click = WebDriverWait(driver, 10).until(
                        EC.element_to_be_clickable(time_link_element_candidate)
                    )
                    logging.info("Ссылка на время (с автором) стала кликабельной.")
                except NoSuchElementException:
                    logging.warning(
                        f"Ссылка на время с автором '{post_author_username}' не найдена. Пробуем общий XPath.")
                except TimeoutException:
                    logging.warning(
                        "Ссылка на время (с автором) найдена, но не стала кликабельной. Пробуем общий XPath.")
                except Exception as e_find_specific:  # Ловим другие ошибки при поиске/ожидании
                    logging.warning(
                        f"Ошибка при поиске/ожидании ссылки на время (с автором): {type(e_find_specific).__name__}. Пробуем общий XPath.")

                # Попытка 2: Если не нашли с автором или не стала кликабельной, пробуем общий XPath
                if not time_link_element_to_click:
                    try:
                        logging.debug(f"Попытка найти ссылку на время по общему XPath: {time_link_xpath_general}")
                        time_link_element_candidate = current_post_element.find_element(By.XPATH,
                                                                                        time_link_xpath_general)

                        logging.info(
                            "Найдена предполагаемая ссылка на время (общий XPath). Прокрутка и ожидание кликабельности...")
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
                                              time_link_element_candidate)
                        time.sleep(uniform(0.5, 1.0))

                        time_link_element_to_click = WebDriverWait(driver, 10).until(
                            EC.element_to_be_clickable(time_link_element_candidate)
                        )
                        logging.info("Ссылка на время (общий XPath) стала кликабельной.")
                    except NoSuchElementException:
                        logging.warning("Ссылка на время поста не найдена даже общим XPath.")
                    except TimeoutException:
                        logging.warning("Ссылка на время (общий XPath) найдена, но не стала кликабельной.")
                    except Exception as e_find_general:
                        logging.warning(
                            f"Ошибка при поиске/ожидании ссылки на время (общий XPath): {type(e_find_general).__name__}.")

                # Шаг клика, если элемент времени был успешно найден и стал кликабельным
                if time_link_element_to_click:
                    try:
                        logging.info("Попытка клика по найденной ссылке времени...")
                        ActionChains(driver).move_to_element(time_link_element_to_click).click().perform()
                        post_clicked_for_navigation = True
                        logging.info("Клик по ссылке времени поста для перехода выполнен.")
                    except Exception as e_click_time:
                        logging.error(
                            f"Ошибка при клике по найденной ссылке времени: {type(e_click_time).__name__} - {e_click_time}",
                            exc_info=True)
                        # Если клик по времени не удался, пробуем клик по всему посту
                        time_link_element_to_click = None  # Сбрасываем, чтобы перейти к запасному варианту

                # Запасной вариант: Клик по всему посту, если не удалось кликнуть по времени
                if not post_clicked_for_navigation:
                    logging.info("Не удалось кликнуть по времени. Используем клик по всему посту (запасной вариант)...")
                    try:
                        # Убедимся, что current_post_element все еще актуален и кликабелен
                        WebDriverWait(driver, 5).until(EC.element_to_be_clickable(current_post_element))
                        ActionChains(driver).move_to_element(current_post_element).click().perform()
                        post_clicked_for_navigation = True
                        logging.info("Клик по всему элементу поста выполнен (запасной вариант).")
                    except Exception as e_fallback_click:
                        logging.error(f"Ошибка даже при запасном клике по всему элементу поста: {e_fallback_click}")

                if not post_clicked_for_navigation:
                    logging.error("НЕ УДАЛОСЬ КЛИКНУТЬ ДЛЯ ПЕРЕХОДА НА ПОСТ. Пропускаем пост.")
                    post_processing_attempts += 1
                    continue

                time.sleep(uniform(1.5, 2.5))

                # --- ОБНОВЛЕННАЯ ЛОГИКА ДЛЯ КЕБАБ-МЕНЮ (УПРОЩЕННАЯ) ---

                logging.info(
                    f"Ожидание и клик по кебаб-меню на странице поста ...")

                driver.find_element(By.XPATH, "/html/body/div[1]/div/div/main/div/div/div/div/div/div[1]/div[3]/button/div").click()

                # Если клик был успешным, продолжаем
                logging.info("Ожидание выпадающего меню...")
                time.sleep(uniform(0.7, 1.2))  # Пауза для появления выпадающего списка (оставляем)

                # --- ШАГ 3: Ожидание и клик по "Delete cast" ---
                WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.XPATH, dropdown_menu_xpath)))
                logging.debug("Выпадающее меню видимо.")

                delete_option = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, delete_cast_option_xpath))
                )
                logging.info("Пункт 'Delete cast' найден и кликабелен. Кликаем...")
                delete_option.click()
                deleted_posts_count += 1
                logging.info(f"Команда 'Delete cast' отправлена. Всего удалено: {deleted_posts_count}.")

                # --- ШАГ 4: Возврат назад ---
                # (Остальная часть этой логики остается прежней)
                logging.info("Возврат на предыдущую страницу (профиль)...")
                time.sleep(uniform(1.0, 2.0))
                driver.back()

                try:
                    WebDriverWait(driver, 15).until(
                        EC.presence_of_element_located((By.XPATH, profile_page_indicator_xpath))
                    )
                    logging.info("Успешно вернулись на страницу профиля (найден индикатор).")
                except TimeoutException:
                    logging.warning(
                        "Не удалось подтвердить возврат на страницу профиля по индикатору. Попытка принудительного перехода.")
                    try:
                        driver.get(original_profile_url)
                        WebDriverWait(driver, 15).until(
                            EC.presence_of_element_located((By.XPATH, profile_page_indicator_xpath)))
                        logging.info("Принудительный переход на страницу профиля выполнен.")
                    except Exception as e_force_nav:
                        logging.error(
                            f"Не удалось принудительно вернуться в профиль: {e_force_nav}. Прерываем удаление.")
                        break


            except StaleElementReferenceException:
                logging.warning(
                    f"Пост для перехода устарел (StaleElement). Вероятно, список обновился. Повторяем поиск.")
            except (TimeoutException, NoSuchElementException) as e_main:
                logging.warning(
                    f"Не удалось обработать пост (Timeout/NoSuchElement на странице поста или в меню): {type(e_main).__name__}. Попытка вернуться назад...")
                try:
                    # Проверяем, на какой странице мы "застряли"
                    # Пытаемся найти индикатор профиля с коротким таймаутом
                    try:
                        WebDriverWait(driver, 3).until(
                            EC.presence_of_element_located((By.XPATH, profile_page_indicator_xpath)))
                        # Если мы здесь, значит, элемент найден - мы на странице профиля
                        logging.info("Уже на странице профиля (индикатор найден). Обновление для нового поиска...")
                        driver.refresh()
                    except TimeoutException:
                        # Если индикатор профиля не найден за 3 секунды, значит мы не на странице профиля
                        logging.info(
                            f"Индикатор профиля не найден. Попытка вернуться на сохраненный URL профиля: {original_profile_url}")
                        driver.get(original_profile_url)
                        # После перехода ждем появления индикатора профиля, чтобы убедиться, что страница загрузилась
                        WebDriverWait(driver, 15).until(
                            EC.presence_of_element_located((By.XPATH, profile_page_indicator_xpath)))
                        logging.info("Успешно вернулись на страницу профиля по URL.")

                    time.sleep(uniform(1.0, 2.0))  # Пауза после возврата/обновления
                except Exception as back_err:
                    logging.error(
                        f"Ошибка при попытке вернуться/обновить страницу профиля после ошибки обработки поста: {back_err}",
                        exc_info=True)
                    break  # Критическая ошибка, если не можем восстановиться, прерываем цикл удаления

            post_processing_attempts += 1
            if deleted_posts_count >= 30:
                logging.info(f"Достигнут лимит в {deleted_posts_count} удаленных постов за сессию.")
                break

        if post_processing_attempts >= max_post_processing_attempts:
            logging.warning("Достигнут максимальный лимит попыток обработки постов.")

        logging.info(f"--- Завершение процесса удаления постов. Всего удалено: {deleted_posts_count} ---")
    except Exception as e:
        logging.error(f'Общая ошибка в функции delete_post: {type(e).__name__} - {e}', exc_info=True)

def scroll_town(driver):
    """Скроллит специфичный элемент на странице Towns."""
    try:
        wait = WebDriverWait(driver, 15)
        element = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div._1b907cj0._3zlyma6ka._3zlyma5w1 > div > div")))

        logging.info("✅ Элемент найден для скроллинга в Towns")

        repeat_count = random.randint(1, 2)
        for _ in range(repeat_count):
            # Прокрутка вверх 2–3 раза
            for _ in range(random.randint(2, 3)):
                offset = random.randint(70, 300)
                driver.execute_script("arguments[0].scrollBy(0, -arguments[1]);", element, offset)
                logging.debug(f"⬆ Прокрутка вверх на {offset}px")
                time.sleep(random.uniform(0.6, 1.2))

            time.sleep(random.uniform(0.5, 1.0))

            # Прокрутка вниз 1–2 раза
            for _ in range(random.randint(1, 2)):
                offset = random.randint(90, 200)
                driver.execute_script("arguments[0].scrollBy(0, arguments[1]);", element, offset)
                logging.debug(f"⬇ Прокрутка вниз на {offset}px")
                time.sleep(random.uniform(0.6, 1.2))

            time.sleep(random.uniform(0.8, 1.5))

    except TimeoutException:
        # Используем конкретное исключение
        logging.warning("❌ Элемент для скроллинга в Towns не найден (Timeout). Проверь CSS-селектор.")
    except NoSuchElementException: # На всякий случай, если элемент пропадет после нахождения
        logging.warning("❌ Элемент для скроллинга в Towns не найден (NoSuchElement).")
    except WebDriverException as e: # Общая ошибка WebDriver
        logging.error(f"⚠ Ошибка WebDriver при скроллинге в Towns: {type(e).__name__} - {e}")
    except Exception as e: # Другие неожиданные ошибки
        logging.error(f"⚠ Неожиданная ошибка при скроллинге в Towns: {type(e).__name__} - {e}", exc_info=True)

def choice_town(driver, excluded_elements=None):
    """Находит и кликает по иконке 'Town'."""
    if excluded_elements is None:
            excluded_elements = []

    logging.info(f"Попытка выбрать новый город, исключая {len(excluded_elements)} уже выбранных элементов.")
    try:
        wait = WebDriverWait(driver, 15)
        # Ваш основной селектор для нахождения блоков городов
        # Это XPath для родительского элемента каждого "города", который содержит картинку
        # Предоставленный вами HTML: самый внешний span для ROBOTown имеет классы _153ynsn0 _3zlyma5ms _3zlyma3ia ... _1b907cjfu
        # Если кликабельным является именно он, то селектор может быть таким:
        # "//span[contains(@class, '_153ynsn0') and contains(@class, '_3zlyma5ms') and .//img[contains(@src, '/space/')]]"
        # Или, если ваш старый "//div[.//img[contains(@src, '/space/')]]" работал, то он должен находить правильные блоки.
        # Давайте для начала вернемся к вашему проверенному селектору для самих блоков:
        town_block_xpath = "//div[.//img[contains(@src, '/space/')]]"

        # Можно добавить ожидание видимости хотя бы одного такого элемента
        wait.until(EC.visibility_of_any_elements_located((By.XPATH, town_block_xpath)))
        all_town_divs = driver.find_elements(By.XPATH, town_block_xpath)

        if not all_town_divs:
            logging.warning("Не найдено ни одного блока города на странице.")
            return None

        # Фильтруем элементы, которые еще не были выбраны
        available_town_divs = [div for div in all_town_divs if div not in excluded_elements]

        if not available_town_divs:
            logging.warning(f"Не найдено НОВЫХ доступных городов (все {len(all_town_divs)} уже были выбраны).")
            return None # Нет доступных новых городов для выбора

        selected_town_div = random.choice(available_town_divs)

        # Попытка извлечь имя для логгирования (не обязательно для работы, но полезно)
        try:
            name_element = selected_town_div.find_element(By.XPATH, ".//span[contains(@class, 'hecx1o4f')]") # Используем XPath из предыдущего обсуждения
            town_name_for_log = name_element.text.strip()
            logging.info(f"Выбран новый город (блок): {town_name_for_log if town_name_for_log else 'Имя не извлечено'}")
        except NoSuchElementException:
            logging.info("Выбран новый город (блок), но имя для лога не извлечено.")

        # Кликаем по выбранному блоку города
        ActionChains(driver).move_to_element(selected_town_div).pause(0.5).click().perform()
        logging.info("Клик по блоку Town выполнен.")
        time.sleep(5) # Пауза после клика
        return selected_town_div # ВОЗВРАЩАЕМ ВЫБРАННЫЙ WebElement

    except TimeoutException:
        logging.warning("Не найдено Towns-иконок/блоков (Timeout) при попытке выбора нового.")
    except WebDriverException as e:
        logging.error(f"Ошибка WebDriver при выборе Town: {type(e).__name__} - {e}")
    except Exception as e:
        logging.error(f"Неожиданная ошибка в choice_town: {type(e).__name__} - {e}", exc_info=True)
    return None





# --- Проект: Warpcast ---
def warpcast(driver, text_file_path, comment_file_path, text_lock, comment_lock, enabled_actions, picture_folder_path, picture_lock):
    """Выполняет действия на сайте Warpcast."""
    logging.info("--- Начало работы с Warpcast ---")

    # --- Навигация ---
    def go_to_followers(driver_inner):
        logging.info("Переход в профиль -> фолловеры...")
        try:
            # Добавлено ожидание кликабельности кнопки профиля
            profile_button = WebDriverWait(driver_inner, 15).until(
                EC.element_to_be_clickable((By.XPATH, "//div[contains(@class, 'cursor-pointer') and .//div[text()='Profile']]"))
            )
            profile_button.click()
            logging.info("Клик по кнопке 'Profile'.")
            time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))

            # Добавлено ожидание кликабельности ссылки фолловеров
            followers_link = WebDriverWait(driver_inner, 15).until(
                EC.element_to_be_clickable((By.XPATH, "//div[@data-state='closed' and .//span[text()='Followers']]"))
            )
            followers_link.click()
            logging.info("Клик по ссылке 'Followers'.")
            # Добавлено ожидание появления элементов списка фолловеров
            WebDriverWait(driver_inner, 15).until(
                EC.presence_of_element_located((By.XPATH, '//div[contains(@class, "flex flex-row justify-between border-default border-b")]'))
            )
            logging.info("Список фолловеров загружен.")
            return True
        except (TimeoutException, NoSuchElementException) as e:
            logging.error(f"Не удалось перейти к фолловерам: {type(e).__name__}")
            return False
        except WebDriverException as e:
            logging.error(f"Ошибка WebDriver при переходе к фолловерам: {type(e).__name__} - {e}")
            return False


    def follower_choice(driver_inner):
        logging.info("Выбор случайного фолловера...")
        try:
            # Небольшой скроллинг для имитации просмотра
            scroll_moves = random.randint(1, 5)
            for _ in range(scroll_moves):
                direction = random.choice([-1, 1])
                scroll_amount = random.randint(50, 350) * direction
                driver_inner.execute_script(f"window.scrollBy(0, {scroll_amount});")
                time.sleep(random.uniform(1, 2))

            # Ожидание наличия хотя бы одного фолловера
            follower_xpath = '//div[contains(@class, "flex flex-row justify-between border-default border-b")]'
            WebDriverWait(driver_inner, 10).until(EC.presence_of_element_located((By.XPATH, follower_xpath)))
            followers = driver_inner.find_elements(By.XPATH, follower_xpath)

            if not followers:
                logging.warning("Фолловеры не найдены в списке.")
                return None # Возвращаем None вместо False для ясности

            selected_follower = random.choice(followers)
            logging.info("Фолловер выбран.")

            # Попытка скрыть кнопки перед кликом (может не сработать, если элемент перекрыт)
            try:
                buttons_in_follower = selected_follower.find_elements(By.TAG_NAME, "button")
                for btn in buttons_in_follower:
                    driver_inner.execute_script("arguments[0].style.pointerEvents = 'none';", btn)
            except StaleElementReferenceException:
                 logging.warning("Не удалось скрыть кнопки у фолловера (StaleElement).")
            except Exception as e_btn:
                 logging.warning(f"Ошибка при скрытии кнопок у фолловера: {e_btn}")


            # Клик по элементу фолловера
            ActionChains(driver_inner).move_to_element(selected_follower).pause(0.5).click().perform()
            logging.info("Клик по контейнеру фолловера выполнен.")

            # Ожидание элемента на странице профиля фолловера
            WebDriverWait(driver_inner, 15).until(
                EC.presence_of_element_located((By.XPATH, '//div[contains(@class, "line-clamp-feed")] | //button[text()="Follow"]')) # Ждем либо пост, либо кнопку Follow
            )
            logging.info("Переход на профиль фолловера подтверждён.")
            return True # Успешный переход
        except TimeoutException:
            logging.warning("Не удалось выбрать фолловера или перейти на его профиль (Timeout).")
            return False
        except WebDriverException as e:
            logging.error(f"Ошибка WebDriver при выборе фолловера: {type(e).__name__} - {e}")
            return False
        except Exception as e:
             logging.error(f"Неожиданная ошибка в follower_choice: {type(e).__name__} - {e}", exc_info=True)
             return False


    # --- Действия ---
    def likes(driver_inner):
        """
        Переходит на главную, скроллит ленту, находит посты,
        случайным образом выбирает НЕОБРАБОТАННЫЙ пост, переходит на него,
        лайкает (от 5 до 10 постов) и возвращается.
        """
        logging.info("--- Начало функции like_feed_posts ---")

        try:
            # --- Шаг 1: Клик по кнопке "Home" ---
            logging.info("Переход на главную страницу (Home)...")
            home_button_xpath = "//a[@title='Home' and @href='/']"
            try:
                home_button = WebDriverWait(driver, 15).until(
                    EC.element_to_be_clickable((By.XPATH, home_button_xpath))
                )
                home_button.click()
                logging.info("Клик по кнопке 'Home' выполнен.")
                post_card_in_feed_xpath_check = "//div[contains(@class, 'px-4 py-2')]//div[@class='relative flex']"
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.XPATH, post_card_in_feed_xpath_check))
                )
                logging.info("Главная лента (предположительно) загружена.")
                time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))
            except Exception as e_home:
                logging.error(f"Не удалось перейти на главную страницу: {type(e_home).__name__} - {e_home}")
                return

            # --- Шаг 2: Скроллинг ленты ---
            num_scrolls = random.randint(2, 4)
            scroll_pixels = random.randint(300, 800)
            logging.info(f"Скроллинг ленты {num_scrolls} раз(а) по ~{scroll_pixels}px...")
            for i in range(num_scrolls):
                driver.execute_script(f"window.scrollBy(0, {scroll_pixels});")
                logging.debug(f"Скролл {i+1}/{num_scrolls}")
                time.sleep(random.uniform(2.5, 4.5))

            # --- Шаг 3 и 4: Поиск постов и лайк (цикл) ---
            post_card_xpath = "//div[contains(@class, 'px-4 py-2')]//div[@class='relative flex']"

            likes_to_set = random.randint(5, 10)
            logging.info(f"Цель: поставить {likes_to_set} лайков.")

            liked_posts_in_session_count = 0 # Счетчик успешно ПОСТАВЛЕННЫХ лайков
            attempted_post_urls = set()     # Множество для URL постов, на которые уже ПЕРЕХОДИЛИ

            total_processing_attempts = 0   # Общий счетчик попыток найти и обработать новый пост
            max_total_attempts = likes_to_set * 5 # Даем больше попыток найти УНИКАЛЬНЫЙ пост

            while liked_posts_in_session_count < likes_to_set and total_processing_attempts < max_total_attempts:
                total_processing_attempts += 1
                logging.info(f"--- Итерация {total_processing_attempts}/{max_total_attempts}. Цель лайков: {liked_posts_in_session_count}/{likes_to_set} ---")

                current_feed_posts_elements = []
                try:
                    time.sleep(random.uniform(0.5, 1.0))
                    current_feed_posts_elements = driver.find_elements(By.XPATH, post_card_xpath)
                    logging.info(f"На текущем экране найдено {len(current_feed_posts_elements)} карточек постов.")
                except Exception as e_find_cards:
                    logging.error(f"Ошибка при поиске карточек постов: {e_find_cards}")
                    time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))
                    continue

                if not current_feed_posts_elements:
                    logging.warning("Карточек постов на экране не найдено. Попытка доп. скролла.")
                    driver.execute_script(f"window.scrollBy(0, {scroll_pixels // 2});")
                    time.sleep(random.uniform(2.0, 3.0))
                    continue

                # --- Выбор необработанного поста ---
                selected_post_card = None
                link_to_click_for_navigation = None
                post_url_to_remember = None

                # Перемешиваем найденные на экране посты, чтобы не всегда брать первый
                random.shuffle(current_feed_posts_elements)

                for card_candidate in current_feed_posts_elements:
                    try:
                        # Пытаемся извлечь URL из ссылки на время
                        temp_author = "temp_author_unknown"
                        try:
                            author_el = card_candidate.find_element(By.XPATH, ".//a[contains(@class, 'font-semibold')]")
                            name_text = author_el.text.strip()
                            if not name_text:
                                href_val = author_el.get_attribute('href')
                                if href_val and '/' in href_val: name_text = href_val.split('/')[-1]
                            if name_text: temp_author = name_text
                        except: pass

                        xpath_time_specific = f".//a[starts-with(@href, '/{temp_author}/') and descendant::div[contains(@class, 'text-faint') and string-length(normalize-space(text())) > 0 and string-length(normalize-space(text())) < 6]]"
                        xpath_time_general = ".//a[descendant::div[contains(@class, 'text-faint') and string-length(normalize-space(text())) > 0 and string-length(normalize-space(text())) < 6]]"

                        potential_time_link = None
                        if temp_author != "temp_author_unknown":
                            try: potential_time_link = card_candidate.find_element(By.XPATH, xpath_time_specific)
                            except NoSuchElementException: pass
                        if not potential_time_link:
                            potential_time_link = card_candidate.find_element(By.XPATH, xpath_time_general)

                        current_url_candidate = potential_time_link.get_attribute('href')

                        if current_url_candidate and current_url_candidate not in attempted_post_urls:
                            selected_post_card = card_candidate
                            link_to_click_for_navigation = potential_time_link
                            post_url_to_remember = current_url_candidate
                            logging.info(f"Выбран необработанный пост с URL: {post_url_to_remember}")
                            break # Нашли подходящий пост, выходим из цикла for card_candidate
                        elif current_url_candidate:
                             logging.debug(f"Пост {current_url_candidate} уже был в attempted_post_urls.")

                    except NoSuchElementException: # Не нашли ссылку на время в этой карточке
                        logging.debug("Не найдена ссылка на время в текущей карточке, пропускаем ее для выбора.")
                    except StaleElementReferenceException:
                        logging.warning("Карточка устарела при поиске ссылки на время для выбора.")
                    except Exception as e_select:
                        logging.warning(f"Ошибка при выборе поста-кандидата: {e_select}")

                if not selected_post_card or not link_to_click_for_navigation:
                    logging.info("Не найдено подходящих необработанных постов на текущем экране. Попытка скролла.")
                    driver.execute_script(f"window.scrollBy(0, {scroll_pixels});") # Скроллим, чтобы найти новые
                    time.sleep(random.uniform(2.5, 4.5))
                    continue

                # Добавляем URL в "попытки", чтобы не обрабатывать его снова
                if post_url_to_remember:
                    attempted_post_urls.add(post_url_to_remember)
                    logging.debug(f"URL {post_url_to_remember} добавлен в attempted_post_urls.")

                # --- Обработка выбранного поста ---
                original_feed_url = driver.current_url
                try:
                    logging.info(f"Переход на пост: {post_url_to_remember if post_url_to_remember else 'URL не извлечен'}")
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", link_to_click_for_navigation)
                    time.sleep(random.uniform(0.5, 1.0))
                    ActionChains(driver).move_to_element(link_to_click_for_navigation).click().perform()

                    # Ожидание загрузки страницы поста
                    action_icons_on_post_page_css = 'div.flex.flex-row.items-center > div.group.cursor-pointer'
                    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, action_icons_on_post_page_css)))
                    logging.info("Страница поста загружена.")
                    time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))

                    # Лайк на странице поста
                    like_action_icons_container_xpath = "//div[contains(@class, 'ml-[-8px]')]"
                    try:
                        WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.XPATH, like_action_icons_container_xpath)))
                        action_icons_parent = driver.find_element(By.XPATH, like_action_icons_container_xpath)
                        clickable_icons = action_icons_parent.find_elements(By.XPATH, "./div[contains(@class, 'cursor-pointer')] | ./button")

                        if len(clickable_icons) >= 3:
                            like_icon_element = clickable_icons[2]
                            is_already_liked = False
                            try:
                                path_element = like_icon_element.find_element(By.TAG_NAME, "path")
                                fill_color = path_element.get_attribute("fill")
                                if fill_color and fill_color.upper() == "#D84F4F":
                                    is_already_liked = True; logging.info("Лайк на этом посту уже стоит.")
                            except: logging.warning("Не удалось найти path для проверки цвета лайка.")

                            if not is_already_liked:
                                logging.info("Лайк не стоит. Попытка поставить лайк...")
                                driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", like_icon_element); time.sleep(0.3)
                                ActionChains(driver).move_to_element(like_icon_element).click().perform()
                                logging.info("Лайк поставлен.")
                                liked_posts_in_session_count += 1 # Увеличиваем счетчик
                                time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))
                            # Если лайк уже стоял, liked_posts_in_session_count не увеличивается
                        else: logging.warning(f"На странице поста найдено только {len(clickable_icons)} иконок, недостаточно для лайка.")
                    except Exception as e_like_action: logging.error(f"Ошибка при попытке лайкнуть пост: {type(e_like_action).__name__} - {e_like_action}")

                except Exception as e_post_processing:
                    logging.error(f"Ошибка при обработке выбранного поста: {type(e_post_processing).__name__} - {e_post_processing}", exc_info=True)

                finally:
                    logging.info("Возврат на страницу ленты...")
                    driver.get(original_feed_url)
                    try:
                        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.XPATH, post_card_xpath)))
                        logging.info("Вернулись на страницу ленты.")
                    except TimeoutException:
                        logging.error("Не удалось подтвердить возврат на страницу ленты. Попытка обновить..."); driver.refresh(); time.sleep(3)
                    time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))

                if liked_posts_in_session_count >= likes_to_set:
                    logging.info(f"Цель в {likes_to_set} лайков достигнута.")
                    scroll_page(driver, duration=6)
                    break

            if total_processing_attempts >= max_total_attempts and liked_posts_in_session_count < likes_to_set:
                logging.warning(f"Достигнут лимит попыток ({max_total_attempts}), но поставлено только {liked_posts_in_session_count}/{likes_to_set} лайков.")

            logging.info(f"Завершена функция like_feed_posts. Всего поставлено лайков в этой сессии: {liked_posts_in_session_count}.")

        except Exception as e_main_func:
            logging.error(f"Общая ошибка в функции like_feed_posts: {type(e_main_func).__name__} - {e_main_func}", exc_info=True)
        finally:
            logging.info("--- Завершение функции like_feed_posts ---")



    def cast(driver_inner, current_text_file_path, current_text_file_lock, current_picture_folder_path, current_picture_file_lock):
        logging.info("Создание нового каста (текст или картинка)...")
        try:
            driver_inner.get("https://warpcast.com/")
            time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))
            cast_button_main = WebDriverWait(driver_inner, 15).until(
                EC.element_to_be_clickable((By.XPATH, "//div[contains(@class, 'sm:flex')]//button[text()='Cast']"))
            )
            cast_button_main.click()
            logging.info("Кнопка 'Cast' (главная) нажата.")
            time.sleep(random.uniform(1.5, 2.5))

            # Ожидание поля ввода текста
            text_input_xpath = "//div[@contenteditable='true' and contains(@class, 'public-DraftEditor-content')]"
            try:
                input_field = WebDriverWait(driver_inner, 10).until(
                    EC.presence_of_element_located((By.XPATH, text_input_xpath))
                )
                logging.info("Окно создания каста открыто, поле ввода найдено.")
            except TimeoutException:
                logging.error("Не удалось найти поле ввода в окне создания каста. Отмена.")
                return

            # --- Рандомный выбор: текст или картинка ---
            post_type = random.choices(['text', 'picture'], weights=[0.5, 0.5], k=1)[0]
            logging.info(f"Выбран тип поста: {post_type}")

            content_successfully_prepared = False

            if post_type == 'text':
                cast_text = get_random_line_and_remove(current_text_file_path, current_text_file_lock)
                if not cast_text:
                    logging.warning("Не удалось получить текст для каста.")
                else:
                    try:
                        ActionChains(driver_inner).move_to_element(input_field).click().send_keys(cast_text).perform()
                        logging.info("Текст для каста введен.")
                        content_successfully_prepared = True
                    except Exception as e_text:
                        logging.error(f"Ошибка при вводе текста: {e_text}")

            elif post_type == 'picture':
                image_path_to_post = get_random_picture(current_picture_folder_path, current_picture_file_lock)

                if not image_path_to_post:
                    logging.warning("Не удалось получить картинку для каста.")
                else:
                    try:
                        file_input_xpath = "//input[@type='file' and @accept='.png,.jpg,.jpeg,.gif']"
                        logging.info(f"Попытка найти скрытый input type='file' по XPath: {file_input_xpath}")
                        file_input_element = driver_inner.find_element(By.XPATH, file_input_xpath)

                        logging.info(
                            f"Прикрепление картинки '{image_path_to_post}' через send_keys на input type='file'...")
                        file_input_element.send_keys(image_path_to_post)
                        logging.info("Команда send_keys для картинки выполнена.")

                        # --- ОЖИДАНИЕ ЗАГРУЗКИ ПРЕВЬЮ (используем версию v4 из предыдущего ответа) ---
                        logging.info("Ожидание появления и полной загрузки превью картинки (до 45 секунд)...")
                        preview_img_tag_xpath = "//img[@alt='Cast image embed']"
                        preview_image_element = None
                        try:
                            preview_image_element = WebDriverWait(driver_inner, 20).until(
                                EC.presence_of_element_located((By.XPATH, preview_img_tag_xpath))
                            )
                            logging.info("Элемент <img> (плейсхолдер/превью) найден в DOM.")

                            def image_fully_loaded_check(driver):
                                try:
                                    current_preview_element = driver.find_element(By.XPATH, preview_img_tag_xpath)
                                    js_checks_pass = driver.execute_script(
                                        "return arguments[0].complete && " +
                                        "typeof arguments[0].naturalWidth !== 'undefined' && " +
                                        "arguments[0].naturalWidth > 0;",
                                        current_preview_element
                                    )
                                    if not js_checks_pass: return False
                                    style_attribute = current_preview_element.get_attribute("style")
                                    if style_attribute and "aspect-ratio" in style_attribute:
                                        return True
                                    return False
                                except (StaleElementReferenceException, NoSuchElementException):
                                    return False

                            WebDriverWait(driver_inner, 45).until(image_fully_loaded_check)

                            content_successfully_prepared = True
                            logging.info("Превью картинки успешно загружено и отображено.")

                            warpcast_processing_pause = random.uniform(3.0, 7.0)
                            logging.info(
                                f"Дополнительная пауза {warpcast_processing_pause:.1f} сек для обработки картинки сервером Warpcast...")
                            time.sleep(warpcast_processing_pause)
                            logging.info("Предполагается, что картинка успешно прикреплена и обработана сервером.")

                        # ... (обработка TimeoutException и других ошибок для превью) ...
                        except TimeoutException:
                            logging.error("Превью картинки не появилось или не загрузилось...")
                            # ... (код скриншота) ...
                        except Exception as e_preview_wait:
                            logging.error(f"Другая ошибка при ожидании превью: {e_preview_wait}", exc_info=True)


                    except NoSuchElementException:
                        logging.error(
                            f"Не удалось найти элемент input type='file'. Проверьте XPath: {file_input_xpath}")
                    except Exception as e_pic:
                        logging.error(f"Ошибка при попытке прикрепления картинки: {e_pic}", exc_info=True)

            # Общая логика отправки поста
            if not content_successfully_prepared:
                logging.error("Контент (текст или картинка) не был подготовлен/добавлен. Отмена каста.")
                return

            time.sleep(random.uniform(0.5, 1.5))
            submit_button_cast_window_xpath = "//button[@title='Cast' and text()='Cast']"
            post_sent_successfully = False
            try:
                submit_button_cast_window = WebDriverWait(driver_inner, 10).until(
                    EC.element_to_be_clickable((By.XPATH, submit_button_cast_window_xpath))
                )
                submit_button_cast_window.click()
                logging.info("Кнопка 'Cast' в окне каста нажата.")

                WebDriverWait(driver_inner, 15).until(
                    EC.invisibility_of_element_located((By.XPATH, submit_button_cast_window_xpath))
                )
                logging.info("Окно каста закрылось (пост предположительно отправлен).")
                post_sent_successfully = True
            # ... (обработка ошибок отправки) ...
            except TimeoutException:
                logging.warning("Окно каста не закрылось или кнопка отправки не стала невидимой.")
            except Exception as e_submit:
                logging.error(f"Ошибка при отправке каста: {e_submit}")

            # --- Удаление картинки ПОСЛЕ попытки отправки каста ---
            if post_type == 'picture' and image_path_to_post:
                if post_sent_successfully:
                    logging.info(f"Пост с картинкой '{image_path_to_post}' отправлен. Удаляем файл.")
                    remove_file_if_exists(image_path_to_post, current_picture_file_lock)
                else:
                    logging.warning(
                        f"Пост с картинкой '{image_path_to_post}' НЕ был успешно отправлен (или не подтверждено). Файл НЕ удаляется.")

            scroll_page(driver_inner, duration=random.randint(3, 6))

        except (TimeoutException, NoSuchElementException) as e:
            logging.error(f"Не удалось создать каст (основная ошибка): {type(e).__name__} - {e}", exc_info=True)
        except Exception as e:
            logging.error(f"Неожиданная ошибка в cast(): {type(e).__name__} - {e}", exc_info=True)


    def follow_followers(driver_inner):
        logging.info("Подписка на пользователей из списка фолловеров...")
        # Выполняем переход и проверяем результат
        if not go_to_followers(driver_inner):
            logging.warning("Не удалось перейти к фолловерам, отмена подписки.")
            return

        try:
            # Находим кнопки Follow (ждем их появления)
            follow_buttons_xpath = "//button[text()='Follow']"
            WebDriverWait(driver_inner, 10).until(EC.presence_of_element_located((By.XPATH, follow_buttons_xpath)))
            buttons = driver_inner.find_elements(By.XPATH, follow_buttons_xpath)
            logging.info(f"Найдено {len(buttons)} кнопок 'Follow'.")

            if not buttons:
                logging.info("Нет доступных кнопок 'Follow' для подписки.")
                return

            # Подписываемся на случайное количество
            num_to_follow = random.randint(1, min(len(buttons), 5)) # Ограничиваем кол-во
            logging.info(f"Попытка подписаться на {num_to_follow} пользователей.")
            followed_count = 0

            # Перемешиваем кнопки, чтобы подписываться не по порядку
            random.shuffle(buttons)

            for button in buttons[:num_to_follow]:
                 # Проверяем видимость и кликабельность перед кликом
                try:
                    # Прокрутка к кнопке
                    driver_inner.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", button)
                    time.sleep(0.5)

                    # Ждем кликабельности именно этой кнопки
                    WebDriverWait(driver_inner, 5).until(EC.element_to_be_clickable(button))

                    # Используем JS клик как более надежный для динамических списков
                    driver_inner.execute_script("arguments[0].click();", button)
                    logging.info(f"Подписка {followed_count+1}/{num_to_follow} выполнена.")
                    followed_count += 1
                    time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))

                except StaleElementReferenceException:
                    logging.warning("Кнопка 'Follow' устарела (StaleElement), пропуск.")
                except ElementClickInterceptedException:
                     logging.warning("Клик по кнопке 'Follow' перехвачен, пропуск.")
                except TimeoutException:
                     logging.warning("Кнопка 'Follow' не стала кликабельной, пропуск.")
                except WebDriverException as wd_e:
                    logging.error(f"Ошибка WebDriver при подписке: {type(wd_e).__name__} - {wd_e}")
                except Exception as e:
                    logging.error(f"Неожиданная ошибка при подписке: {type(e).__name__} - {e}", exc_info=True)

        except TimeoutException:
            logging.info("Кнопки 'Follow' не найдены в списке фолловеров.")
        except Exception as e:
            logging.error(f"Общая ошибка в follow_followers(): {type(e).__name__} - {e}", exc_info=True)


    def comment_follower(driver_inner, file_path, lock):
        logging.info("Комментирование/репост поста случайного фолловера...")
        try:
            if not go_to_followers(driver_inner):
                logging.warning("Не удалось перейти к фолловерам, отмена комментирования.")
                return
            if not follower_choice(driver_inner):
                logging.warning("Не удалось выбрать фолловера, отмена комментирования.")
                # Возвращаемся назад, чтобы не оставаться на пустой странице
                driver_inner.back()
                time.sleep(1)
                return

            # --- Вложенные функции для comment_follower ---
            def find_post_and_click(driver_local):
                logging.info("Поиск поста для взаимодействия (через клик по дате)...")
                scroll_tries = 0
                # post_to_navigate будет хранить карточку поста (для логов и как запасной вариант)
                post_to_navigate = None
                # time_link_to_click будет хранить сам элемент ссылки на время
                time_link_to_click = None

                while scroll_tries <= 5:  # Оставляем лимит скролла
                    # XPath для КАРТОЧКИ поста. Должен находить корневой элемент каждого поста в ленте.
                    # Этот XPath ищет div, который является кликабельным (cursor-pointer) и содержит
                    # информацию о пользователе (например, ссылку с href, содержащую имя пользователя)
                    # ИЛИ содержит картинку аватара. Это должно быть достаточно общим.
                    # ВАЖНО: Адаптируйте этот XPath, если он не находит ваши карточки постов.
                    post_card_xpath = "//div[contains(@class, 'cursor-pointer') and (.//a[contains(@class, 'font-semibold') and contains(@href, '/')] or .//img[contains(@alt, 'avatar')]) and not(ancestor::div[@role='dialog'])]"
                    # Добавил not(ancestor::div[@role='dialog']) чтобы исключить посты во всплывающих окнах, если такие есть

                    potential_post_cards = driver_local.find_elements(By.XPATH, post_card_xpath)
                    logging.debug(f"Найдено {len(potential_post_cards)} потенциальных карточек постов на экране.")

                    valid_posts_with_time_link = []
                    for card_element in potential_post_cards:
                        try:
                            # Пытаемся найти ссылку на время ВНУТРИ этой карточки
                            post_author_username = "unknown_author"  # Значение по умолчанию
                            try:
                                author_link_element = card_element.find_element(By.XPATH,
                                                                                ".//a[contains(@class, 'font-semibold')]")
                                temp_username = author_link_element.text.strip()
                                if not temp_username:
                                    href_value = author_link_element.get_attribute('href')
                                    if href_value and '/' in href_value:
                                        temp_username = href_value.split('/')[-1]
                                if temp_username:  # Присваиваем только если удалось извлечь
                                    post_author_username = temp_username
                            except NoSuchElementException:
                                logging.debug("Автор для карточки не найден, будет использован общий XPath времени.")

                            # XPath для ссылки на время
                            # 1. С проверкой автора (более точный)
                            # 2. Общий (если автор неизвестен или первый XPath не сработал)
                            specific_time_link_xpath = (
                                f".//a[starts-with(@href, '/{post_author_username}/') and "
                                f"descendant::div[contains(@class, 'text-faint') and "
                                f"string-length(normalize-space(text())) > 0 and string-length(normalize-space(text())) < 6]]"
                            )
                            general_time_link_xpath = ".//a[descendant::div[contains(@class, 'text-faint') and string-length(normalize-space(text())) > 0 and string-length(normalize-space(text())) < 6]]"

                            found_time_link_element = None
                            try:
                                if post_author_username != "unknown_author":  # Пробуем специфичный, если есть автор
                                    found_time_link_element = card_element.find_element(By.XPATH,
                                                                                        specific_time_link_xpath)
                                if not found_time_link_element:  # Если специфичный не сработал или автора нет
                                    found_time_link_element = card_element.find_element(By.XPATH,
                                                                                        general_time_link_xpath)
                            except NoSuchElementException:
                                pass  # Ссылку на время не нашли в этой карточке

                            if found_time_link_element:
                                # Сохраняем и саму карточку, и ссылку на время
                                valid_posts_with_time_link.append(
                                    {'card': card_element, 'time_link': found_time_link_element})
                        except StaleElementReferenceException:
                            logging.warning("Карточка поста устарела во время поиска ссылки на время, пропускаем.")
                            continue  # Переходим к следующей карточке

                    logging.debug(f"Найдено {len(valid_posts_with_time_link)} постов со ссылкой на время.")

                    if len(valid_posts_with_time_link) >= 1:
                        # Выбираем случайный из первых 3 (или сколько есть)
                        candidates = valid_posts_with_time_link[:min(len(valid_posts_with_time_link), 3)]
                        selected_post_data = random.choice(candidates)
                        post_to_navigate = selected_post_data['card']  # Вся карточка (для лога/запасного клика)
                        time_link_to_click = selected_post_data['time_link']  # Ссылка на время для клика
                        break  # Выходим из цикла while, пост найден

                    logging.debug("Постов со ссылкой на время не найдено на текущем экране, скроллим ниже...")
                    driver_local.execute_script("window.scrollBy(0, 1000);")
                    time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))  # Глобальные паузы
                    scroll_tries += 1

                # --- Конец цикла while ---

                if post_to_navigate and time_link_to_click:
                    post_text_for_log = "не удалось извлечь текст"
                    try:
                        # Пытаемся извлечь текст из карточки для лога
                        text_element = post_to_navigate.find_element(By.XPATH,
                                                                     ".//div[contains(@class, 'line-clamp-feed')]")
                        post_text_for_log = text_element.text[:30].replace("\n", " ") + "..."
                    except:
                        pass
                    logging.info(f"Найден пост для взаимодействия: '{post_text_for_log}'. Клик по ссылке времени...")

                    try:
                        # Прокручиваем к ссылке времени и кликаем
                        driver_local.execute_script(
                            "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", time_link_to_click)
                        time.sleep(random.uniform(0.3, 0.7))  # Пауза после скролла

                        ActionChains(driver_local).move_to_element(time_link_to_click).click().perform()
                        logging.info("Клик по ссылке времени поста для перехода выполнен.")

                        # Ожидание загрузки страницы поста (например, появления иконок действий)
                        WebDriverWait(driver_local, 15).until(
                            EC.presence_of_element_located(
                                (By.CSS_SELECTOR, 'div.flex.flex-row.items-center > div.group.cursor-pointer'))
                            # Этот селектор должен соответствовать иконкам действий на странице поста
                        )
                        logging.info("Страница поста (предположительно) загружена.")
                        return True  # Успешный переход

                    except StaleElementReferenceException:
                        logging.warning("Ссылка на время устарела перед кликом. Повторите попытку.")
                        return False
                    except ElementClickInterceptedException:
                        logging.warning("Клик по ссылке времени перехвачен. Попытка JS клика...")
                        try:
                            driver_local.execute_script("arguments[0].click();", time_link_to_click)
                            logging.info("Клик по ссылке времени через JS выполнен.")
                            WebDriverWait(driver_local, 15).until(EC.presence_of_element_located(
                                (By.CSS_SELECTOR, 'div.flex.flex-row.items-center > div.group.cursor-pointer')))
                            logging.info("Страница поста (предположительно) загружена после JS клика.")
                            return True
                        except Exception as js_e:
                            logging.error(f"JS-клик по ссылке времени тоже не удался: {js_e}")
                            return False
                    except (TimeoutException, NoSuchElementException) as click_err:
                        logging.error(
                            f"Не удалось кликнуть по ссылке времени или дождаться загрузки страницы поста: {type(click_err).__name__}")
                        return False
                    except WebDriverException as wd_e:
                        logging.error(f"Ошибка WebDriver при клике по ссылке времени: {type(wd_e).__name__} - {wd_e}")
                        return False
                else:
                    logging.warning("Не найдено ни одного поста со ссылкой на время после скролла.")
                    return False

            def like(driver_local):
                logging.info("Попытка поставить лайк посту (с проверкой по цвету fill)...")
                time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))  # Пауза перед действием
                try:
                    # --- Шаг 1: Найти все иконки действий ---
                    icons_selector = "div.ml-\\[-8px\\] div.rounded-full"
                    WebDriverWait(driver_local, 10).until(
                        EC.visibility_of_element_located((By.CSS_SELECTOR, icons_selector))
                    )
                    icons = driver_local.find_elements(By.CSS_SELECTOR, icons_selector)
                    logging.debug(f"Найдено {len(icons)} иконок действий.")

                    # --- Шаг 2: Проверить, достаточно ли иконок и получить нужную ---
                    if len(icons) >= 3:
                        like_icon_div = icons[2]  # Получаем DIV иконки лайка
                        logging.debug("Предполагаемый DIV элемента лайка (по индексу) найден.")

                        # --- Шаг 3: Проверить статус лайка (по цвету fill) ---
                        is_already_liked = False
                        try:
                            # Ищем элемент path ВНУТРИ найденного div'а
                            path_element = like_icon_div.find_element(By.TAG_NAME, "path")
                            fill_color = path_element.get_attribute("fill")
                            logging.debug(f"Цвет fill у path элемента лайка: {fill_color}")

                            # Проверяем, соответствует ли цвет цвету активного лайка
                            # Используем .upper() для сравнения без учета регистра, на всякий случай
                            if fill_color and fill_color.upper() == "#D84F4F":
                                is_already_liked = True
                                logging.info("Лайк уже стоит (fill='#D84F4F').")
                            else:
                                is_already_liked = False
                                logging.info("Лайк не стоит (fill не красный).")

                        except NoSuchElementException:
                            logging.warning("Не удалось найти элемент 'path' внутри иконки лайка для проверки цвета.")
                            return  # Выходим, если не можем проверить
                        except Exception as check_err:
                            logging.warning(f"Не удалось проверить статус лайка по цвету fill: {check_err}")
                            return  # Выходим из функции лайка

                        # --- Шаг 4: Кликнуть, только если лайк НЕ стоит ---
                        if not is_already_liked:
                            logging.info("Попытка клика для установки лайка...")
                            try:
                                # Кликаем по самому DIV'у иконки
                                driver_local.execute_script(
                                    "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", like_icon_div)
                                time.sleep(0.3)
                                ActionChains(driver_local).move_to_element(like_icon_div).pause(0.3).click().perform()
                                logging.info("Лайк поставлен.")
                                time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))  # Пауза после успешного лайка
                            except ElementClickInterceptedException:
                                logging.warning("Клик по лайку перехвачен. Попытка JS клика...")
                                try:
                                    driver_local.execute_script("arguments[0].click();", like_icon_div)
                                    logging.info("Лайк поставлен через JS.")
                                    time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))
                                except Exception as js_e:
                                    logging.error(f"JS клик по лайку тоже не удался: {js_e}")
                            except Exception as click_err:
                                logging.error(
                                    f"Не удалось кликнуть по иконке лайка: {type(click_err).__name__} - {click_err}")

                    else:
                        # Недостаточно иконок
                        logging.warning(f"Найдено только {len(icons)} иконок, недостаточно для лайка (ожидалось >= 3).")

                # Обработка общих ошибок поиска или доступа к элементам
                except TimeoutException:
                    logging.warning("Не удалось найти иконки действий (Timeout).")
                except IndexError:
                    logging.error("Ошибка индекса при доступе к иконке лайка (возможно, структура изменилась).")
                except (NoSuchElementException, StaleElementReferenceException) as e:
                    logging.error(f"Ошибка элемента при попытке поставить лайк: {type(e).__name__}")
                except WebDriverException as wd_e:
                    logging.error(f"Ошибка WebDriver при лайке: {type(wd_e).__name__} - {wd_e}")
                except Exception as e:
                    logging.error(f"Неожиданная ошибка в функции like(): {type(e).__name__} - {e}", exc_info=True)

            def comm_or_repost(driver_local, comment_text):
                time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))
                logging.info("Попытка комментирования или репоста...")
                if not comment_text:
                    logging.warning("Нет текста для комментария/репоста. Действие отменено.")
                    return

                try:
                    # --- Общие шаги: Найти иконки ---
                    icons_css = 'div.flex.flex-row.items-center > div.group.cursor-pointer'
                    WebDriverWait(driver_local, 15).until(
                        EC.presence_of_all_elements_located((By.CSS_SELECTOR, icons_css))
                    )
                    icons = driver_local.find_elements(By.CSS_SELECTOR, icons_css)
                    logging.debug(f"Найдено {len(icons)} иконок действий.")

                    if len(icons) < 2:
                        logging.warning(f"Найдено только {len(icons)} иконок действий, недостаточно для комм/репоста.")
                        return

                    # --- Выбор действия ---
                    choice = random.choice(['comment', 'repost'])
                    logging.info(f"Выбрано действие: {choice}")

                    # --- Выполнение Комментария ---
                    if choice == 'comment':
                        target_icon = icons[0]  # Предполагаем, что первая иконка - коммент
                        logging.info("Выбрана иконка комментария.")

                        # Клик по иконке комментария
                        driver_local.execute_script(
                            "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", target_icon)
                        time.sleep(0.3)
                        target_icon.click()
                        logging.info("Клик по иконке комментария выполнен.")
                        time.sleep(0.5)

                        # Ожидание поля ввода комментария
                        input_field_xpath = '//div[@contenteditable="true" and contains(@class, "public-DraftEditor-content")]'
                        logging.info("Ожидание поля ввода для комментария...")
                        # Используем ожидание наличия из старого кода (с таймаутом 10 сек)
                        input_field = WebDriverWait(driver_local, 10).until(
                            EC.presence_of_element_located((By.XPATH, input_field_xpath))
                        )
                        logging.info("Поле ввода найдено.")

                        # ---> НАЧАЛО: СТАРЫЙ JS ДЛЯ ВСТАВКИ ТЕКСТА (ДЛЯ КОММЕНТАРИЯ) <---
                        logging.info("Вставка текста комментария (старый метод JS)...")
                        driver_local.execute_script("""
                            const editor = arguments[0];
                            const text = arguments[1];
                            const selection = window.getSelection();
                            // Проверяем, есть ли вообще выделение и диапазоны
                            if (selection && selection.rangeCount > 0) {
                                const range = selection.getRangeAt(0);
                                range.deleteContents(); // Удаляем выделенное содержимое (если есть)
                                const textNode = document.createTextNode(text);
                                range.insertNode(textNode); // Вставляем текст как узел
                                // Перемещаем курсор в конец вставленного текста (опционально, но часто полезно)
                                range.setStartAfter(textNode);
                                range.collapse(true);
                                selection.removeAllRanges(); // Снимаем выделение
                                selection.addRange(range); // Восстанавливаем курсор
                            } else {
                                // Запасной вариант, если нет выделения (например, просто ставим текст)
                                // Этого блока может и не быть, если range всегда есть в contenteditable
                                editor.textContent = text;
                            }
                            // Триггерим событие input в любом случае
                            editor.dispatchEvent(new Event('input', { bubbles: true }));
                        """, input_field, comment_text)
                        logging.info("Текст комментария вставлен (старый метод JS).")


                        time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))

                        # Нажатие кнопки Reply
                        reply_button_xpath = '//button[@title="Reply" and text()="Reply"]'
                        logging.info("Ожидание кнопки 'Reply'...")
                        # Используем ожидание кликабельности
                        reply_button = WebDriverWait(driver_local, 10).until(
                            EC.element_to_be_clickable((By.XPATH, reply_button_xpath))
                        )
                        # Прокручиваем перед кликом
                        driver_local.execute_script(
                            "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", reply_button)
                        time.sleep(0.3)
                        reply_button.click()
                        logging.info("Комментарий отправлен.")

                        # Ожидание закрытия окна/модалки
                        try:
                            WebDriverWait(driver_local, 10).until(
                                EC.invisibility_of_element_located((By.XPATH, reply_button_xpath))
                            )
                            logging.info("Окно комментария закрылось.")
                        except TimeoutException:
                            logging.warning("Окно комментария не закрылось автоматически.")

                    # --- Выполнение Репоста (Цитирования) ---
                    elif choice == 'repost':
                        target_icon = icons[1]  # Предполагаем, что вторая иконка - репост/цитата
                        logging.info("Выбрана иконка репоста/цитаты.")

                        # Клик по иконке репоста/цитаты
                        driver_local.execute_script(
                            "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", target_icon)
                        time.sleep(0.3)
                        target_icon.click()
                        logging.info("Клик по иконке репоста/цитаты выполнен.")
                        time.sleep(0.5)

                        # Ожидание и клик по кнопке "Quote"
                        quote_button_xpath = '//button[.//span[contains(text(), "Quote")]]'
                        logging.info("Ожидание кнопки 'Quote'...")
                        # Используем ожидание кликабельности
                        quote_button = WebDriverWait(driver_local, 10).until(
                            EC.element_to_be_clickable((By.XPATH, quote_button_xpath))
                        )
                        # Прокручиваем перед кликом
                        driver_local.execute_script(
                            "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", quote_button)
                        time.sleep(0.3)
                        quote_button.click()
                        logging.info("Нажата кнопка 'Quote'.")
                        time.sleep(2)

                        # Ожидание поля ввода для цитаты
                        input_field_xpath = '//div[@contenteditable="true" and contains(@class, "public-DraftEditor-content")]'
                        logging.info("Ожидание поля ввода для цитаты...")
                        # Используем ожидание наличия
                        input_field = WebDriverWait(driver_local, 10).until(
                            EC.presence_of_element_located((By.XPATH, input_field_xpath))
                        )
                        logging.info("Поле ввода найдено.")

                        # ---> НАЧАЛО: СТАРЫЙ JS ДЛЯ ВСТАВКИ ТЕКСТА (ДЛЯ РЕПОСТА) <---
                        logging.info("Вставка текста для цитаты (старый метод JS)...")
                        driver_local.execute_script("""
                            const editor = arguments[0];
                            const text = arguments[1];
                            const selection = window.getSelection();
                            // Проверяем, есть ли вообще выделение и диапазоны
                            if (selection && selection.rangeCount > 0) {
                                const range = selection.getRangeAt(0);
                                range.deleteContents(); // Удаляем выделенное содержимое (если есть)
                                const textNode = document.createTextNode(text);
                                range.insertNode(textNode); // Вставляем текст как узел
                                // Перемещаем курсор в конец вставленного текста (опционально, но часто полезно)
                                range.setStartAfter(textNode);
                                range.collapse(true);
                                selection.removeAllRanges(); // Снимаем выделение
                                selection.addRange(range); // Восстанавливаем курсор
                            } else {
                                // Запасной вариант, если нет выделения
                                editor.textContent = text;
                            }
                            // Триггерим событие input в любом случае
                            editor.dispatchEvent(new Event('input', { bubbles: true }));
                        """, input_field, comment_text)
                        logging.info("Текст для цитаты вставлен (старый метод JS).")


                        time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))

                        # Нажатие кнопки Cast (для репоста)
                        cast_button_xpath = '//button[@title="Cast" and text()="Cast"]'
                        logging.info("Ожидание кнопки 'Cast' для отправки цитаты...")
                        # Используем ожидание кликабельности
                        cast_button = WebDriverWait(driver_local, 10).until(
                            EC.element_to_be_clickable((By.XPATH, cast_button_xpath))
                        )
                        # Прокручиваем перед кликом
                        driver_local.execute_script(
                            "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", cast_button)
                        time.sleep(0.3)
                        cast_button.click()
                        logging.info("Цитата (репост с комментом) отправлена.")

                        # Ожидание закрытия окна/модалки
                        try:
                            WebDriverWait(driver_local, 10).until(
                                EC.invisibility_of_element_located((By.XPATH, cast_button_xpath))
                            )
                            logging.info("Окно репоста закрылось.")
                        except TimeoutException:
                            logging.warning("Окно репоста не закрылось автоматически.")

                # --- Общая обработка ошибок для всей функции ---
                except TimeoutException as te:
                    logging.error(f"Ошибка Timeout во время {choice}: {te}")
                    try:
                        screenshot_path = f'error_{choice}_timeout_{time.strftime("%Y%m%d_%H%M%S")}.png'
                        driver_local.save_screenshot(screenshot_path)
                        logging.info(f"Скриншот ошибки Timeout сохранен: {screenshot_path}")
                    except Exception as screen_err:
                        logging.error(f"Не удалось сохранить скриншот: {screen_err}")
                except (NoSuchElementException, ElementClickInterceptedException, StaleElementReferenceException) as e:
                    logging.error(f"Ошибка элемента во время {choice}: {type(e).__name__} - {e}")
                except WebDriverException as wd_e:
                    if "has no size and location" in str(wd_e):
                        logging.error(f"Конкретная ошибка 'no size and location' при {choice}: {wd_e}")
                    else:
                        logging.error(f"Ошибка WebDriver при {choice}: {type(wd_e).__name__} - {wd_e}")
                except Exception as e:
                    logging.error(f"Неожиданная ошибка в comm_or_repost ({choice}): {type(e).__name__} - {e}",
                                  exc_info=True)


            # --- Основная логика comment_follower ---
            if find_post_and_click(driver_inner):
                # Успешно перешли на страницу поста
                action = random.choice(['like', 'comment_repost']) # Разделил лайк и комм/репост
                logging.info(f"Выбрано действие на странице поста: {action}")

                if action == 'like':
                    like(driver_inner)
                else: # 'comment_repost'
                    # Получаем текст для комментария/репоста с блокировкой
                    comment_text = get_random_line_and_remove(file_path, lock) # <--- Текст получаем здесь
                    if comment_text: # Проверяем, что текст получен
                        comm_or_repost(driver_inner, comment_text) # <--- Передаем текст сюда
                    else:
                        logging.warning("Не удалось получить текст комментария/репоста. Действие пропущено.")
            else:
                logging.warning("Не удалось найти и кликнуть пост фолловера.")

            # Возвращаемся на предыдущую страницу (список фолловеров или профиль)
            logging.info("Возврат на предыдущую страницу...")
            driver_inner.back()
            scroll_page(driver, duration=5)
            time.sleep(random.uniform(1, 3))

        except Exception as e:
            logging.error(f"Общая ошибка в comment_follower(): {type(e).__name__} - {e}", exc_info=True)
            try:
                # Попытка вернуться назад в случае непредвиденной ошибки
                driver_inner.back()
                time.sleep(1)
            except Exception as back_e:
                 logging.error(f"Не удалось вернуться назад после ошибки: {back_e}")


    def run_multiple_interactions(driver_inner, file_path, lock):
        """ Запускает несколько итераций comment_follower """
        repeat_count = random.randint(1, 4)
        logging.info(f"Запуск {repeat_count} итераций взаимодействия с фолловерами.")
        for i in range(repeat_count):
            logging.info(f"--- Итерация взаимодействия {i + 1}/{repeat_count} ---")
            try:
                # Передаем путь к файлу и блокировку
                comment_follower(driver_inner, file_path, lock)
            except Exception as e:
                # Логгируем ошибку на конкретной итерации
                logging.error(f"Ошибка на итерации {i + 1} в run_multiple_interactions: {type(e).__name__} - {e}", exc_info=True)
                # Можно добавить driver.refresh() или возврат на главную в случае серьезной ошибки
                try:
                    logging.info("Попытка обновить страницу после ошибки...")
                    driver_inner.refresh()
                    time.sleep(5)
                except WebDriverException as refresh_e:
                    logging.error(f"Не удалось обновить страницу: {refresh_e}")
                    # Если даже обновить не можем, возможно, стоит прервать цикл
                    break
            # Пауза между итерациями
            logging.info(f"Пауза после итерации {i+1}...")
            time.sleep(random.uniform(PAUSE_MIN * 1.5, PAUSE_MAX * 1.5)) # Увеличенная пауза


    def follow_new_followers(driver_inner):
        logging.info("Запуск 'follow_new_followers' (имитация старой логики)...")

        # --- ШАГ 1: Перейти к списку ВАШИХ фолловеров ---
        if not go_to_followers(driver_inner):
            logging.warning("Не удалось перейти к списку своих фолловеров. Отмена.")
            return

        # --- ШАГ 2: Выбрать случайного фолловера и перейти на его профиль ---
        # Используем существующую follower_choice из нового кода
        if not follower_choice(driver_inner):
            logging.warning("Не удалось выбрать фолловера или перейти на его профиль. Отмена.")
            # Вернемся назад, если не получилось
            try:
                driver_inner.back(); time.sleep(1)
            except Exception:
                pass
            return
        # Теперь мы находимся на странице профиля СЛУЧАЙНОГО пользователя

        # --- ШАГ 3: Выполнить логику старой функции follow() ---
        # Она снова кликала на Followers, что переведет нас на список фолловеров ВЫБРАННОГО пользователя
        logging.info("Переход к списку фолловеров ВЫБРАННОГО пользователя...")
        try:
            # Используем XPath из старого кода
            followers_xpath = "//div[@data-state='closed' and .//span[text()='Followers']]"
            # Ожидаем и кликаем
            followers_div = WebDriverWait(driver_inner, 15).until(
                EC.element_to_be_clickable((By.XPATH, followers_xpath))
            )
            followers_div.click()
            logging.info("Клик по 'Followers' на странице профиля выполнен.")
            # Ожидаем загрузки списка фолловеров этого пользователя
            WebDriverWait(driver_inner, 15).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//button[text()='Follow'] | //div[contains(@class, 'border-b')]"))
                # Ждем кнопку Follow или просто разделитель списка
            )
            logging.info("Список фолловеров выбранного пользователя загружен.")
            time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))  # Пауза как в старом коде
        except (TimeoutException, NoSuchElementException, ElementClickInterceptedException) as e:
            logging.error(f"Не удалось перейти к списку фолловеров выбранного пользователя: {type(e).__name__}")
            # Попробуем вернуться назад и завершить
            try:
                driver_inner.back(); time.sleep(1)
            except Exception:
                pass
            return

        # --- Подписка на фолловеров выбранного пользователя (Первая волна из старого 'follow') ---
        logging.info("Фаза 1: Подписка на фолловеров выбранного пользователя...")
        follow_buttons_xpath = "//button[text()='Follow']"
        buttons = []
        try:
            # Даем немного времени на прогрузку списка
            WebDriverWait(driver_inner, 5).until(EC.presence_of_element_located((By.XPATH, follow_buttons_xpath)))
            buttons = driver_inner.find_elements(By.XPATH, follow_buttons_xpath)
        except TimeoutException:
            logging.info("Кнопки 'Follow' не найдены в списке фолловеров выбранного пользователя.")
            # Не выходим, т.к. вторая фаза еще есть

        total = len(buttons)
        logging.info(f"Найдено {total} кнопок 'Follow' в списке.")

        if total > 0:
            # Логика выбора количества из старого кода
            count_to_click = min(random.randint(5, 10), total)
            logging.info(f"Попытка нажать {count_to_click} кнопок 'Follow'.")
            followed_count = 0
            # НЕ перемешиваем, чтобы имитировать старый код (он брал первые [i])
            for i in range(count_to_click):
                # На каждой итерации ищем заново, т.к. элементы могут меняться
                current_buttons = driver_inner.find_elements(By.XPATH, follow_buttons_xpath)
                if i >= len(current_buttons):
                    logging.warning(f"Кнопки закончились раньше, чем ожидалось (на итерации {i + 1}).")
                    break
                button = current_buttons[i]
                try:
                    driver_inner.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
                                                button)
                    time.sleep(0.5)  # Пауза как в старом коде
                    # Явное ожидание кликабельности
                    WebDriverWait(driver_inner, 5).until(EC.element_to_be_clickable(button))
                    # JS клик как в старом коде
                    driver_inner.execute_script("arguments[0].click();", button)
                    logging.info(f"[Фаза 1 | {i + 1}/{count_to_click}] Кнопка 'Follow' нажата.")
                    followed_count += 1
                    time.sleep(random.uniform(1, 2))  # Пауза как в старом коде
                except (StaleElementReferenceException, TimeoutException, ElementClickInterceptedException) as e:
                    logging.warning(f"[Фаза 1 | {i + 1}] Ошибка при нажатии кнопки 'Follow': {type(e).__name__}")
                except WebDriverException as wd_e:
                    logging.error(f"[Фаза 1 | {i + 1}] Ошибка WebDriver: {type(wd_e).__name__}")
                except Exception as e:
                    logging.error(f"[Фаза 1 | {i + 1}] Неожиданная ошибка: {type(e).__name__}", exc_info=True)
        else:
            logging.info("Нет кнопок 'Follow' для нажатия в Фазе 1.")

        # --- Переход в раздел Following выбранного пользователя ---
        logging.info("Фаза 2: Переход в раздел 'Following' выбранного пользователя...")
        try:
            following_link = WebDriverWait(driver_inner, 15).until(
                EC.element_to_be_clickable((By.XPATH, "//div[contains(@class, 'text-base') and text()='Following']"))
            )
            following_link.click()
            logging.info("Перешли в раздел Following выбранного пользователя.")
            # Ожидание загрузки списка Following
            WebDriverWait(driver_inner, 15).until(
                EC.presence_of_element_located((By.XPATH, "//button[text()='Following' or text()='Follow']"))
            )
            logging.info("Раздел 'Following' выбранного пользователя загружен.")
            time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))  # Пауза
        except (TimeoutException, NoSuchElementException, ElementClickInterceptedException) as e:
            logging.warning(f"Не удалось перейти в раздел 'Following' выбранного пользователя: {type(e).__name__}")
            # Попробуем вернуться назад и завершить
            try:
                driver_inner.back(); time.sleep(1)
            except Exception:
                pass
            return

        # --- Подписка в разделе Following выбранного пользователя (Вторая волна из старого 'follow') ---
        logging.info("Фаза 2: Подписка в разделе 'Following'...")
        count_to_click_f2 = 0
        attempts = 0
        max_clicks = random.randint(5, 10)  # Как в старом коде
        logging.info(f"Попытка нажать до {max_clicks} кнопок 'Follow' в разделе Following.")

        # Используем XPath, он точнее.
        follow_buttons_xpath_f2 = "//button[text()='Follow']"

        while count_to_click_f2 < max_clicks and attempts < 20:  # Условия как в старом коде
            buttons_f2 = driver_inner.find_elements(By.XPATH, follow_buttons_xpath_f2)

            if not buttons_f2:  # Если кнопки Follow закончились
                logging.info("Кнопки 'Follow' в разделе Following закончились.")
                break  # Выходим из цикла

            # Берем первую доступную кнопку.
            button_to_click = buttons_f2[0]
            attempts += 1  # Увеличиваем попытки здесь

            try:
                driver_inner.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
                                            button_to_click)
                time.sleep(1)  # Пауза как в старом коде
                # Ждем кликабельности
                WebDriverWait(driver_inner, 5).until(EC.element_to_be_clickable(button_to_click))
                # JS клик как в старом коде
                driver_inner.execute_script("arguments[0].click();", button_to_click)
                logging.info(f"[{count_to_click_f2 + 1}/{max_clicks}] Нажата кнопка 'Follow' в разделе Following.")
                count_to_click_f2 += 1
                time.sleep(random.uniform(1, 2))  # Пауза как в старом коде
            except StaleElementReferenceException:
                logging.warning(
                    f"[{count_to_click_f2 + 1}] Пропущена кнопка из-за устаревшего элемента (или др. ошибки).")
                time.sleep(1)  # Пауза как в старом коде
            except (TimeoutException, ElementClickInterceptedException) as e:
                logging.warning(f"[{count_to_click_f2 + 1}] Пропущена кнопка из-за ошибки: {type(e).__name__}")
                time.sleep(1)
            except WebDriverException as wd_e:
                logging.error(f"[Фаза 2 Following | {count_to_click_f2 + 1}] Ошибка WebDriver: {type(wd_e).__name__}")
                time.sleep(1)
            except Exception as e:
                logging.error(f"[Фаза 2 Following | {count_to_click_f2 + 1}] Неожиданная ошибка: {type(e).__name__}",
                              exc_info=True)
                time.sleep(1)

        # --- Возврат назад ---
        logging.info("Завершены подписки в 'follow_new_followers', возврат назад...")
        try:
            driver_inner.back()  # Возврат из раздела Following
            time.sleep(1)
            driver_inner.back()  # Возврат из профиля пользователя
            time.sleep(1)
            driver_inner.back()  # Возврат из списка ваших фолловеров (возможно, лишний?)
            time.sleep(1)
            logging.info("Возврат на предыдущие страницы выполнен.")
        except Exception as back_e:
            logging.error(f"Ошибка при возврате назад в 'follow_new_followers': {back_e}")


    # --- Вызов основных функций Warpcast ---
    logging.info(f"Определение порядка для разрешенных действий Warpcast: {enabled_actions}")

    # --- Шаг 1: Выполнить delete_post, если он разрешен и является первым ---
    if 'delete_post' in enabled_actions:
        logging.info("--- Выполнение приоритетного действия: delete_post ---")
        try:
            delete_post(driver)  # Передаем driver
            action_pause = random.uniform(PAUSE_MIN * 1.5, PAUSE_MAX * 1.5)
            logging.info(f"Пауза после действия delete_post: {action_pause:.1f} сек.")
            time.sleep(action_pause)
        except Exception as e:
            logging.error(f"Ошибка при выполнении действия delete_post: {type(e).__name__} - {e}", exc_info=True)
            return
    else:
        logging.info("Действие delete_post не разрешено в конфигурации или не будет выполняться первым.")

    # --- Шаг 2: Подготовить список ОСТАЛЬНЫХ разрешенных действий для рандомизации ---
    # Все возможные действия, КРОМЕ delete_post, которые могут быть рандомизированы
    possible_randomizable_actions = ["cast", "follow_followers", "run_multiple_interactions", "follow_new_followers", "likes"]

    # Фильтруем их: берем только те, что есть в enabled_actions И не являются delete_post
    remaining_enabled_actions = [action for action in possible_randomizable_actions if action in enabled_actions]

    if not remaining_enabled_actions:
        if 'delete_post' not in enabled_actions:
            logging.warning("Нет разрешенных действий для Warpcast в конфигурации!")
        logging.info("--- Завершение работы с Warpcast (Нет других действий для рандомизации после delete_post) ---")
        return

    # --- Шаг 3: Логика рандомизации для ОСТАЛЬНЫХ действий ---
    logging.info(f"Определение случайного порядка для остальных действий: {remaining_enabled_actions}")

    # 3.1. Определяем, сколько раз запускать run_multiple_interactions (если он среди ОСТАЛЬНЫХ разрешенных)
    num_runs_multi = 0
    # Создаем копию списка для безопасного удаления во время итерации или просто фильтрации
    core_actions_names = list(remaining_enabled_actions)

    if 'run_multiple_interactions' in core_actions_names:
        num_runs_multi = random.randint(1, 2)
        logging.info(f"run_multiple_interactions (среди остальных) будет выполнен {num_runs_multi} раз.")
        # Удаляем его из списка, чтобы потом вставить нужное количество раз
        core_actions_names = [action for action in core_actions_names if action != 'run_multiple_interactions']
    else:
        logging.info("run_multiple_interactions не входит в остальные разрешенные действия для рандомизации.")

    # 3.2. Перемешиваем ОСТАВШИЕСЯ базовые действия
    random.shuffle(core_actions_names)
    logging.debug(f"Оставшиеся базовые действия (имена) после перемешивания: {core_actions_names}")

    # 3.3. Собираем итоговый список, вставляя run_multiple_interactions (если нужно)
    actions_to_run_names = list(core_actions_names)  # Копируем оставшиеся базовые

    # Вставляем run_multiple_interactions нужное количество раз
    for _ in range(num_runs_multi):
        if not actions_to_run_names:  # Если базовых действий не было, а только RMI
            actions_to_run_names.append('run_multiple_interactions')
        else:
            insertion_point = random.randint(0, len(actions_to_run_names))
            actions_to_run_names.insert(insertion_point, 'run_multiple_interactions')

    if not actions_to_run_names:
        logging.warning("Итоговый список рандомизируемых действий для Warpcast (после delete_post) пуст.")
        logging.info("--- Завершение работы с Warpcast ---")
        return

    logging.debug(f"Итоговый список рандомизированных действий для выполнения: {actions_to_run_names}")

    # --- Шаг 4: Выполнение ОСТАЛЬНЫХ действий по именам ---
    logging.info(f"Начало выполнения последовательности ОСТАЛЬНЫХ действий Warpcast: {actions_to_run_names}")
    for i, action_name in enumerate(actions_to_run_names):
        logging.info(f"--- Выполнение действия {i + 1}/{len(actions_to_run_names)}: {action_name} ---")
        try:
            # Вызываем ВНУТРЕННЮЮ функцию по имени
            if action_name == 'cast':
                cast(driver, text_file_path, text_lock, picture_folder_path, picture_lock)
            elif action_name == 'follow_followers':
                follow_followers(driver)
            elif action_name == 'follow_new_followers':
                follow_new_followers(driver)
            elif action_name == 'run_multiple_interactions':
                run_multiple_interactions(driver, comment_file_path, comment_lock)
            elif action_name == 'likes':
                likes(driver)
            else:
                # Этого не должно произойти, если фильтрация верна
                logging.warning(f"Неизвестное или неразрешенное действие попало в цикл: {action_name}")
                continue

            action_pause = random.uniform(PAUSE_MIN * 1.5, PAUSE_MAX * 1.5)
            logging.info(f"Пауза после действия {action_name}: {action_pause:.1f} сек.")
            time.sleep(action_pause)
        except Exception as e:
            logging.error(f"Ошибка при выполнении действия {action_name}: {type(e).__name__} - {e}", exc_info=True)

    logging.info("--- Завершение работы с Warpcast (Конфигурируемый порядок) ---")


# --- Проект: Towns ---
def towns(driver, text_gen_config, enabled_actions):
    """Выполняет действия на сайте Towns."""
    logging.info("--- Начало работы с Towns ---")

    # Вложенная функция оставлена по запросу
    def bober(driver_inner):
        """Кликает на объект в Towns."""
        logging.info("Запуск функции 'bober'...")
        try:
            time.sleep(5)
            wait = WebDriverWait(driver_inner, 15)

            # Ожидание первого элемента (иконка?)
            logging.info("Ожидаем первый элемент ('bober')...")
            first_element_xpath = "//span[contains(@class, 'v57w73y')]//img[contains(@class, '_153ynsn0')]"
            first_element = wait.until(EC.presence_of_element_located((By.XPATH, first_element_xpath)))
            logging.info("Первый элемент найден.")

            # Клик по первому элементу
            ActionChains(driver_inner).move_to_element(first_element).pause(0.5).click().perform()
            logging.info("Первый элемент кликнут.")
            time.sleep(2)

            # Проверка на таймер (если есть, выходим из 'bober')
            try:
                timer_xpath = "//span[p[contains(text(), '⏰')]]"
                WebDriverWait(driver_inner, 3).until(EC.presence_of_element_located((By.XPATH, timer_xpath)))
                logging.info("Обнаружен таймер ⏰ — клик по объекту запрещен. Выход из 'bober'.")
                try:
                    first_element_again = driver_inner.find_element(By.XPATH, first_element_xpath)
                    ActionChains(driver_inner).move_to_element(first_element_again).pause(0.5).click().perform()
                    logging.info("Повторный клик по первому элементу для закрытия окна выполнен.")
                    time.sleep(2)
                except (NoSuchElementException, StaleElementReferenceException) as close_err:
                    logging.warning(f"Не удалось выполнить повторный клик для закрытия окна: {type(close_err).__name__}")
                return
            except TimeoutException:
                logging.info("Таймер ⏰ не найден, продолжаем...")
            except NoSuchElementException:
                 logging.info("Таймер ⏰ не найден (NoSuchElement), продолжаем...")


            # Поиск кликабельного круга/объекта
            logging.info("Поиск кликабельного объекта (круга)...")
            clickable_element_xpath = "//span[contains(@style, 'position: absolute') and contains(@style, 'cursor')]"
            try:
                clickable_element = WebDriverWait(driver_inner, 15).until(
                    EC.element_to_be_clickable((By.XPATH, clickable_element_xpath))
                )
                logging.info("Кликабельный объект найден.")

                # ---> НАЧАЛО: Расчет координат (делаем ДО попыток клика) <---
                location = clickable_element.location
                size = clickable_element.size
                logging.debug(f"Размер объекта: {size['width']}x{size['height']}, расположение: {location}")

                # Расчет отступов с защитой от нулевых размеров
                # (0 else 5): Если ширина > 0, берем 15%, иначе берем 5 пикселей
                margin_x = int(size.get('width', 0) * 0.15) if size.get('width', 0) > 0 else 5
                margin_y = int(size.get('height', 0) * 0.15) if size.get('height', 0) > 0 else 5
                logging.debug(f"Расчетные отступы (margin_x, margin_y): ({margin_x}, {margin_y})")

                # Проверка что отступы не больше половины размера
                max_offset_x = size.get('width', 0) - margin_x
                max_offset_y = size.get('height', 0) - margin_y

                abs_x, abs_y = None, None # Инициализируем координаты
                can_click_by_coords = False

                if max_offset_x > margin_x and max_offset_y > margin_y :
                    offset_x = random.randint(margin_x, max_offset_x)
                    offset_y = random.randint(margin_y, max_offset_y)
                    logging.debug(f"Выбраны случайные смещения внутри элемента: x={offset_x}, y={offset_y}")

                    # Получаем абсолютные координаты для JS-клика
                    abs_x = location.get('x', 0) + offset_x
                    abs_y = location.get('y', 0) + offset_y
                    logging.debug(f"Абсолютные координаты для JS-клика: x={abs_x}, y={abs_y}")
                    can_click_by_coords = True # Флаг, что координаты рассчитаны успешно
                else:
                    logging.warning("Не удалось вычислить безопасные координаты для клика (слишком маленький элемент?). Клик по координатам будет пропущен.")
                # ---> КОНЕЦ: Расчет координат <---

                click_successful = False # Флаг успешного клика

                # Способ 1: Стандартный .click() с имитацией наведения на СЛУЧАЙНУЮ точку и увеличенными паузами
                try:
                    # Добавляем увеличенную случайную короткую паузу перед действием
                    pre_hover_pause = random.uniform(0.3, 0.8)  # Увеличили диапазон
                    logging.debug(f"Пауза перед наведением: {pre_hover_pause:.2f} сек.")
                    time.sleep(pre_hover_pause)

                    # Имитируем наведение мыши на СЛУЧАЙНУЮ точку со случайной задержкой
                    hover_pause = random.uniform(0.5, 1.5)  # Увеличили диапазон
                    logging.info(f"Попытка 1: Наведение на элемент ({hover_pause:.2f} сек) и клик через .click()...")
                    actions = ActionChains(driver_inner)

                    # --- Наведение на случайную точку (если координаты рассчитаны) или на центр ---
                    if can_click_by_coords:
                        # offset_x, offset_y были рассчитаны ранее
                        logging.debug(f"Наведение на случайную точку со смещением (x={offset_x}, y={offset_y})")
                        actions.move_to_element_with_offset(clickable_element, offset_x, offset_y)
                    else:
                        # Если координаты не рассчитались, наводим на центр по умолчанию
                        logging.debug("Наведение на центр элемента (координаты не были рассчитаны).")
                        actions.move_to_element(clickable_element)
                    # --- Конец выбора точки наведения ---

                    actions.pause(hover_pause)  # Выдерживаем паузу после наведения
                    actions.perform()  # Выполняем наведение и паузу

                    # Выполняем стандартный клик ПОСЛЕ наведения
                    clickable_element.click()
                    logging.info("Клик выполнен через .click() после имитации наведения.")
                    click_successful = True

                except MoveTargetOutOfBoundsException:
                    # Эта ошибка может возникнуть при move_to_element_with_offset, если координаты плохие
                    logging.warning(
                        f"ActionChains (наведение) не удалось: рассчитанные координаты вне границ? Пробуем другие методы...")
                except ElementClickInterceptedException as e1:
                    logging.warning(
                        f"Стандартный клик (.click()) после наведения не сработал (перехвачен): {e1}. Пробуем другие методы...")
                except WebDriverException as wd_e:
                    logging.warning(
                        f"Ошибка WebDriver при наведении/.click(): {type(wd_e).__name__}. Пробуем другие методы...")
                except Exception as e_click:
                    logging.warning(
                        f"Неожиданная ошибка при наведении/.click(): {type(e_click).__name__}. Пробуем другие методы...")

                # Способ 2: ActionChains с КЛИКОМ ПО ЦЕНТРУ элемента (Запасной вариант 1)
                # (Этот блок остается без изменений, как запасной)
                if not click_successful:
                    try:
                        logging.info(f"Попытка 2: Клик через ActionChains (по центру)...")
                        actions = ActionChains(driver_inner)
                        actions.move_to_element(clickable_element)
                        actions.click()
                        actions.perform()
                        logging.info("Клик выполнен через ActionChains (по центру).")
                        click_successful = True
                    # ... (обработка ошибок для ActionChains остается прежней) ...
                    except ElementClickInterceptedException as e_aci_int:
                        logging.warning(f"ActionChains клик (центр) перехвачен: {e_aci_int}. Пробуем другие методы...")
                    except WebDriverException as e_aci_wd:
                        logging.warning(
                            f"ActionChains (центр) ошибка WebDriver: {type(e_aci_wd).__name__}. Пробуем другие методы...")
                    except Exception as e_aci:
                        logging.warning(
                            f"Неожиданная ошибка ActionChains (центр): {type(e_aci).__name__} - {e_aci}. Пробуем другие методы...")

                # Способ 3: JavaScript-клик по элементу (Запасной вариант 2)
                # (Этот блок остается без изменений)
                if not click_successful:
                    try:
                        logging.info("Попытка 3: Клик через JS по элементу (arguments[0].click())...")
                        driver_inner.execute_script("arguments[0].click();", clickable_element)
                        logging.info("Клик выполнен через JS по элементу.")
                        click_successful = True
                    except Exception as e2:
                        logging.error(f"JS-клик по элементу тоже не сработал: {e2}")

                # Способ 4: JavaScript-клик по координатам (Запасной вариант 3 - менее надежный)
                # (Этот блок остается без изменений)
                if not click_successful and can_click_by_coords:
                    try:
                        logging.info("Попытка 4: Клик через JS по координатам...")
                        driver_inner.execute_script(f"""
                                            var event = new MouseEvent('click', {{ /* ... */ clientX: {abs_x}, clientY: {abs_y} }});
                                            var element = document.elementFromPoint({abs_x}, {abs_y});
                                            if (element) {{ element.dispatchEvent(event); }}
                                            else {{ console.error('Элемент не найден по координатам {abs_x}, {abs_y}'); }}
                                        """)
                        logging.info("Клик выполнен через JS по координатам.")
                        click_successful = True
                    except Exception as e3:
                        logging.warning(f"Клик по координатам через JS тоже не удался: {e3}")

                # ---> КОНЕЦ: Попытки клика <---

                # Если ни один способ не сработал
                if not click_successful:
                    logging.error("Не удалось выполнить клик по объекту ни одним из способов.")
                    return  # Выходим, если кликнуть не удалось

                time.sleep(3)

                # Проверка на всплывающее окно подтверждения оплаты
                logging.info("Проверка наличия окна подтверждения оплаты...")
                try:
                    confirm_window_xpath = '//*[@id="above-app-progress-root"]/span/span/div/span'
                    confirm_window = WebDriverWait(driver_inner, 10).until(
                        EC.presence_of_element_located((By.XPATH, confirm_window_xpath))
                    )
                    logging.info("Всплывающее окно подтверждения оплаты найдено.")

                    original_window = None
                    try:
                        if len(driver_inner.window_handles) > 1:
                             original_window = driver_inner.current_window_handle
                             logging.info("Обнаружено несколько окон, попытка переключения на последнее...")
                             driver_inner.switch_to.window(driver_inner.window_handles[-1])
                             logging.info(f"Переключились на окно: {driver_inner.title}")
                        else:
                             logging.info("Окно оплаты, похоже, является модальным (на той же странице). Переключение не требуется.")
                    except Exception as switch_err:
                         logging.warning(f"Ошибка при попытке переключения на окно оплаты (возможно, это модальное окно): {switch_err}")

                    time.sleep(2)

                    # Поиск и клик по кнопке оплаты
                    pay_button_xpath = '//button[.//text()[normalize-space()="Pay with ETH"]]'
                    try:
                        button_element = WebDriverWait(driver_inner, 15).until(
                            EC.element_to_be_clickable((By.XPATH, pay_button_xpath))
                        )
                        logging.info("Кнопка оплаты найдена и кликабельна.")

                        pay_click_successful = False
                        try:
                            logging.info("Попытка клика по кнопке оплаты через JS по координатам...")
                            button_location = button_element.location
                            button_size = button_element.size
                            logging.debug(f"Размер кнопки: {button_size}, расположение: {button_location}")

                            # Расчет отступов для кнопки с защитой
                            # (10 else 2): Если высота > 10, берем 10%, иначе 2 пикселя
                            btn_margin_x = int(button_size.get('width', 0) * 0.1) if button_size.get('width', 0) > 10 else 2
                            btn_margin_y = int(button_size.get('height', 0) * 0.1) if button_size.get('height', 0) > 10 else 2
                            logging.debug(f"Расчетные отступы для кнопки (btn_margin_x, btn_margin_y): ({btn_margin_x}, {btn_margin_y})")

                            max_btn_offset_x = button_size.get('width', 0) - btn_margin_x
                            max_btn_offset_y = button_size.get('height', 0) - btn_margin_y

                            if max_btn_offset_x > btn_margin_x and max_btn_offset_y > btn_margin_y:
                                btn_offset_x = random.randint(btn_margin_x, max_btn_offset_x)
                                btn_offset_y = random.randint(btn_margin_y, max_btn_offset_y)
                                logging.debug(f"Выбраны смещения внутри кнопки: x={btn_offset_x}, y={btn_offset_y}")

                                btn_abs_x = button_location.get('x', 0) + btn_offset_x
                                btn_abs_y = button_location.get('y', 0) + btn_offset_y
                                logging.debug(f"Абсолютные координаты для клика по кнопке: x={btn_abs_x}, y={btn_abs_y}")

                                driver_inner.execute_script(f"""
                                    var event = new MouseEvent('click', {{
                                        bubbles: true,
                                        cancelable: true,
                                        view: window,
                                        clientX: {btn_abs_x},
                                        clientY: {btn_abs_y}
                                    }});
                                    var element = document.elementFromPoint({btn_abs_x}, {btn_abs_y});
                                    if (element) {{ element.dispatchEvent(event); }}
                                    else {{ console.error('Элемент не найден по координатам {btn_abs_x}, {btn_abs_y}'); }}
                                """)
                                logging.info("Клик по кнопке оплаты выполнен через JS по координатам.")
                                pay_click_successful = True
                            else:
                                 logging.warning("Не удалось вычислить координаты для клика по кнопке (слишком маленькая?). Пробуем JS клик по элементу.")

                        except Exception as coord_click_err:
                             logging.warning(f"Ошибка при клике по координатам кнопки: {coord_click_err}. Пробуем JS клик по элементу...")

                        if not pay_click_successful:
                             try:
                                 logging.info("Попытка клика по кнопке оплаты через JS по элементу...")
                                 driver_inner.execute_script("arguments[0].click();", button_element)
                                 logging.info("Клик по кнопке оплаты выполнен через JS по элементу.")
                                 pay_click_successful = True
                             except Exception as js_click_err:
                                 logging.error(f"JS клик по элементу кнопки оплаты тоже не удался: {js_click_err}")

                        if pay_click_successful:
                             # Ожидание исчезновения кнопки/окна
                            try:
                                WebDriverWait(driver_inner, 15).until(
                                    EC.invisibility_of_element_located((By.XPATH, pay_button_xpath))
                                )
                                logging.info("Окно оплаты, предположительно, закрылось.")
                            except TimeoutException:
                                logging.warning("Окно оплаты не закрылось после клика.")
                        else:
                             logging.error("Не удалось кликнуть по кнопке оплаты.")
                             # Попытка нажать Enter
                             try:
                                 logging.info("Попытка нажать Enter для подтверждения...")
                                 body = driver_inner.find_element(By.TAG_NAME, 'body')
                                 body.send_keys(Keys.ENTER)
                                 logging.info("Клавиша Enter нажата.")
                             except Exception as key_error:
                                 logging.error(f"Ошибка при нажатии Enter: {key_error}")


                    except TimeoutException:
                         # Обработка TimeoutException для кнопки оплаты, включая нажатие Enter
                         logging.warning("Кнопка оплаты 'Pay with ETH' не найдена или не кликабельна (Timeout).")
                         try:
                             logging.info("Кнопка не найдена, попытка нажать Enter для подтверждения...")
                             body = driver_inner.find_element(By.TAG_NAME, 'body')
                             body.send_keys(Keys.ENTER)
                             logging.info("Клавиша Enter нажата.")
                         except Exception as key_error:
                             logging.error(f"Ошибка при нажатии Enter: {key_error}")

                    except WebDriverException as wd_e_pay:
                         logging.error(f"Ошибка WebDriver при клике на кнопку оплаты: {type(wd_e_pay).__name__}")
                    except Exception as e_pay:
                         logging.error(f"Неожиданная ошибка при клике на кнопку оплаты: {type(e_pay).__name__}", exc_info=True)

                    finally:
                        # Переключение обратно на исходное окно, если нужно
                        if original_window:
                             try:
                                 logging.info("Возврат на исходное окно...")
                                 driver_inner.switch_to.window(original_window)
                                 logging.info("Успешно вернулись на исходное окно.")
                             except Exception as switch_back_err:
                                 logging.error(f"Не удалось вернуться на исходное окно: {switch_back_err}")


                except TimeoutException:
                    logging.info("Всплывающее окно подтверждения оплаты не найдено.")


            except TimeoutException:
                logging.warning("Кликабельный объект (круг) не найден (Timeout). Селектор по стилю ненадежен!")
            except WebDriverException as wd_e_click:
                logging.error(f"Ошибка WebDriver при поиске/клике объекта: {type(wd_e_click).__name__}")
            except Exception as e_click_obj:
                 logging.error(f"Неожиданная ошибка при поиске/клике объекта: {type(e_click_obj).__name__}", exc_info=True)

        except Exception as e:
            logging.error(f"Непредвиденная ошибка в функции 'bober': {type(e).__name__} - {e}", exc_info=True)


    def text(driver_inner, generator_config):
        """Генерирует и отправляет текст в чат Towns."""
        logging.info("Запуск функции 'text' (отправка сообщения в Towns)...")
        channel_name = None
        try:
            # Получение названия канала
            channel_element_xpath = "//span[contains(@class, '_153ynsn0')]/p/span[contains(@class, 'hecx1o4f')]"
            channel_element = WebDriverWait(driver_inner, 15).until(
                EC.visibility_of_element_located((By.XPATH, channel_element_xpath))
            )
            channel_name = channel_element.text.strip()
            if channel_name:
                 logging.info(f"Название канала Towns: {channel_name}")
            else:
                 logging.warning("Не удалось получить текст названия канала (элемент пуст).")
                 return # Выходим, если имя канала не получено

        except TimeoutException:
             logging.error("Не удалось найти элемент с названием канала Towns (Timeout).")
             return
        except WebDriverException as e:
            logging.error(f"Ошибка WebDriver при получении названия канала: {type(e).__name__} - {e}")
            return
        except Exception as e:
             logging.error(f"Неожиданная ошибка при получении названия канала: {type(e).__name__} - {e}", exc_info=True)
             return # Выходим, если не можем получить имя

        # Генерация текста с использованием переданной конфигурации
        generated_text = generate_text_openrouter(
            channel_name,
            generator_config['api_key'],
            generator_config['model'],
            generator_config['system_prompt']
        )

        if not generated_text:
            logging.warning(f"Пропускаем отправку сообщения в '{channel_name}', так как текст не сгенерирован.")
            return

        logging.info(f"Сгенерированный текст для '{channel_name}': {generated_text[:50]}...")

        try:
            # Найти поле ввода и вставить текст
            input_box_css = "div[contenteditable='true']" # Селектор оставлен
            # Добавлено ожидание поля ввода
            input_box = WebDriverWait(driver_inner, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, input_box_css))
            )
            logging.info("Поле ввода сообщения найдено.")

            # ---> НАЧАЛО: Возвращен явный фокус <---
            try:
                logging.debug("Установка фокуса на поле ввода через JS...")
                driver_inner.execute_script("arguments[0].focus();", input_box)
                time.sleep(0.2) # Короткая пауза после фокуса
            except Exception as focus_err:
                logging.warning(f"Не удалось явно установить фокус: {focus_err}")
            # ---> КОНЕЦ: Возвращен явный фокус <---

            # Вставляем текст через ActionChains (как в оригинале)
            logging.info("Вставка текста сообщения через ActionChains...")
            ActionChains(driver_inner).move_to_element(input_box).click().send_keys(generated_text).perform()
            logging.info("Текст сообщения вставлен.")
            time.sleep(0.5) # Пауза оставлена

            # Ждём кнопку отправки и кликаем по ней
            submit_button_css = 'button[data-testid="submit"]'
            submit_button = WebDriverWait(driver_inner, 15).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, submit_button_css))
            )
            submit_button.click()
            logging.info("Сообщение в Towns отправлено.")
            time.sleep(5) # Пауза оставлена

            # Добавлено: ожидание, пока поле ввода очистится или кнопка станет неактивной
            try:
                WebDriverWait(driver_inner, 10).until(
                    lambda d: not d.find_element(By.CSS_SELECTOR, input_box_css).text.strip()
                )
                logging.info("Поле ввода очистилось после отправки.")
            except TimeoutException:
                logging.warning("Поле ввода не очистилось после отправки сообщения.")
            except (NoSuchElementException, StaleElementReferenceException):
                 logging.warning("Не удалось проверить очистку поля ввода после отправки.")

        except (TimeoutException, NoSuchElementException, ElementClickInterceptedException) as e:
             logging.error(f"Ошибка при отправке сообщения в Towns: {type(e).__name__}")
        except WebDriverException as e:
             logging.error(f"Ошибка WebDriver при отправке сообщения: {type(e).__name__} - {e}")
        except Exception as e:
             logging.error(f"Неожиданная ошибка при отправке сообщения: {type(e).__name__} - {e}", exc_info=True)


    # --- Вызов основных функций Towns ---
    # Собираем конфигурацию для генератора текста
    text_gen_details = {
        'api_key': text_gen_config.get('api_key'),
        'model': text_gen_config.get('model'),
        'system_prompt': text_gen_config.get('system_prompt')
    }

    # --- Логика рандомизации Towns ---
    logging.info(f"Определение порядка для разрешенных действий Towns: {enabled_actions}")

    can_choice = 'choice_town' in enabled_actions
    can_text = 'text' in enabled_actions
    can_scroll = 'scroll_town' in enabled_actions
    can_bober = 'bober' in enabled_actions

    if not can_choice:
        logging.warning("Действие 'choice_town' не разрешено. Невозможно выполнить основной цикл Towns.")
        if can_bober:  # Если только bober разрешен, выполняем его
            try:
                logging.info("--- Выполнение действия: bober (т.к. choice_town не разрешен) ---")
                bober(driver)
                time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))
            except Exception as e:
                logging.error(f"Ошибка при выполнении bober: {e}", exc_info=True)
        logging.info("--- Завершение работы с Towns (choice_town не разрешен) ---")
        return

    num_main_loops = random.randint(3, 5)
    logging.info(f"Будет выполнено до {num_main_loops} основных циклов 'выбрать город -> одно действие'.")

    visited_town_elements = []  # Список для хранения WebElement'ов уже выбранных городов
    actual_loops_performed = 0
    bober_performed_this_run = False  # Флаг, чтобы bober выполнился только один раз

    for i in range(num_main_loops):
        logging.info(f"--- Начало цикла Towns {i + 1}/{num_main_loops} ---")

        # 1. Выбрать город (с исключением уже выбранных)
        logging.info("--- Выполнение действия: choice_town ---")
        selected_town_element = choice_town(driver, visited_town_elements)  # Внутренняя

        if selected_town_element:
            visited_town_elements.append(selected_town_element)
            logging.info(f"Город успешно выбран. Всего выбрано: {len(visited_town_elements)}.")
            time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))  # Пауза после выбора

            # 2. Выполнить ОДНО случайное действие (text или scroll_town)
            possible_inner_actions = []
            if can_text: possible_inner_actions.append('text')
            if can_scroll: possible_inner_actions.append('scroll_town')

            if possible_inner_actions:
                chosen_inner_action = random.choice(possible_inner_actions)
                logging.info(f"--- Выполнение внутреннего действия: {chosen_inner_action} ---")
                try:
                    if chosen_inner_action == 'text':
                        text(driver, text_gen_details)  # Внутренняя
                    elif chosen_inner_action == 'scroll_town':
                        scroll_town(driver)  # Внутренняя
                    time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))  # Пауза после внутреннего действия
                except Exception as e:
                    logging.error(f"Ошибка при выполнении {chosen_inner_action}: {e}", exc_info=True)
                    # Решаем, прерывать ли весь цикл Towns или только этот основной цикл
                    # break # Прервать все циклы Towns
                    # continue # Перейти к следующему основному циклу (выбору города)
            else:
                logging.info("Нет разрешенных text/scroll для выполнения в этом цикле.")

            actual_loops_performed += 1

            # 3. Вставить 'bober' ОДИН РАЗ за весь запуск towns, где-то в середине
            # Например, после половины запланированных основных циклов, если он еще не выполнен
            # И если это не последний цикл (чтобы не был всегда в конце)
            if can_bober and not bober_performed_this_run and (
                    actual_loops_performed >= num_main_loops // 2) and actual_loops_performed < num_main_loops:
                # Условие можно упростить: вставить после N-го цикла, например, после 2-го, если всего 3-5 циклов.
                # Или просто случайным образом после какого-то цикла, но только один раз.
                # Более простой вариант: вставить после первого выполненного цикла, если циклов больше одного.
                if actual_loops_performed == 1 and num_main_loops > 1:  # Вставим после первого цикла, если их несколько
                    logging.info("--- Выполнение действия: bober (вставлено после первого цикла) ---")
                    try:
                        bober(driver)  # Внутренняя
                        bober_performed_this_run = True
                        time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))
                    except Exception as e:
                        logging.error(f"Ошибка при выполнении bober: {e}", exc_info=True)

        else:
            logging.warning("Не удалось выбрать новый город (choice_town вернул None). Завершение циклов Towns.")
            break  # Прерываем выполнение ВСЕХ дальнейших циклов Towns

    # Если bober не был выполнен между циклами (например, был только 1 цикл, или условие не сработало)
    # и он разрешен, выполним его в конце (если был хоть один успешный выбор города)
    if can_bober and not bober_performed_this_run and actual_loops_performed > 0:
        logging.info("--- Выполнение действия: bober (в конце, т.к. не был выполнен ранее) ---")
        try:
            bober(driver)
            time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))
        except Exception as e:
            logging.error(f"Ошибка при выполнении bober в конце: {e}", exc_info=True)

    logging.info("--- Завершение работы с Towns (Упрощенная логика) ---")




# --- Рабочая функция для одного профиля (потока) ---
def run_profile_tasks(ads_id, config_data, locks):
    """
    Запускает браузер для ПЕРЕДАННОГО ads_id (БЕЗ позиционирования)
    и выполняет задачи Warpcast/Towns согласно структуре пользователя и конфигу действий.
    """
    thread_name = threading.current_thread().name
    # Используем ads_id напрямую, без worker_index
    logging.info(f"Поток {thread_name} начинает работу с назначенным ID: {ads_id}")

    # Пауза перед запуском профиля
    start_delay_min, start_delay_max = config_data.get("profile_start_delay_range", [0, 0])
    if start_delay_max > 0:
        start_delay = random.uniform(start_delay_min, start_delay_max)
        logging.info(f"[{ads_id}] Пауза перед запуском: {start_delay:.1f} сек...")
        time.sleep(start_delay)

    driver = None
    session_started = False

    try:
        # --- Запуск браузера AdsPower БЕЗ позиционирования ---
        # Формирование параметров для API /browser/start (БЕЗ launch_args/позиции)
        api_params = {
            "user_id": ads_id,
            "open_tabs": "0",
            "ip_tab": "0",
            # "launch_args": json.dumps([...]) # <-- УДАЛЕНО
        }
        open_url = f"http://local.adspower.net:50325/api/v1/browser/start"

        logging.info(f"[{ads_id}] Попытка запуска профиля через API...")
        logging.debug(f"[{ads_id}] Параметры API: {api_params}") # Теперь без launch_args

        try:
            # Отправляем GET запрос с (упрощенными) параметрами
            resp = requests.get(open_url, params=api_params, timeout=120).json()

            if resp.get("code") != 0:
                logging.error(f"[{ads_id}] Ошибка API AdsPower при запуске: {resp.get('msg', 'Нет сообщения об ошибке')}")
                logging.error(f"[{ads_id}] Полный ответ API: {resp}")
                return
            logging.info(f"[{ads_id}] API AdsPower успешно вернул данные.")
            session_started = True

            chrome_options = Options()
            chrome_options.add_experimental_option("debuggerAddress", resp["data"]["ws"]["selenium"])
            service = Service(executable_path=resp["data"]["webdriver"])
            driver = webdriver.Chrome(service=service, options=chrome_options)
            logging.info(f"[{ads_id}] WebDriver инициализирован.")
            driver.implicitly_wait(5)

        # Обработка ошибок запуска
        except requests.exceptions.Timeout: logging.error(f"[{ads_id}] API Timeout."); return
        except requests.exceptions.RequestException as req_err: logging.error(f"[{ads_id}] API Request Error: {req_err}"); return
        except KeyError as key_err: logging.error(f"[{ads_id}] API KeyError: {key_err}. Ответ: {resp}"); return
        except WebDriverException as wd_err:
             logging.error(f"[{ads_id}] WebDriver Init Error: {wd_err}")
             if session_started: # Закрываем сессию, если она стартанула, но драйвер упал
                 close_url = f"http://local.adspower.net:50325/api/v1/browser/stop?user_id={ads_id}"
                 try: requests.get(close_url, timeout=30)
                 except Exception: pass
             return
        except Exception as start_err:
             logging.error(f"[{ads_id}] Unexpected Start Error: {type(start_err).__name__} - {start_err}", exc_info=True)
             if session_started: # Аналогично закрываем
                 close_url = f"http://local.adspower.net:50325/api/v1/browser/stop?user_id={ads_id}"
                 try: requests.get(close_url, timeout=30)
                 except Exception: pass
             return

        # --- Выполнение задач ---
        if driver:
            run_mode = config_data.get("run_mode", "both").lower()
            pause_after_min, pause_after_max = config_data.get("project_completion_pause_range", [60, 120])
            warpcast_actions_to_run = config_data.get("warpcast_enabled_actions", [])
            towns_actions_to_run = config_data.get("towns_enabled_actions", [])

            try:
                # --- Запуск Warpcast ---
                if run_mode in ["warpcast", "both"]:
                    logging.info(f"[{ads_id}] Запуск задач Warpcast (режим: {run_mode})...")
                    driver.get("https://warpcast.com/")
                    time.sleep(5)
                    logging.info(f"[{ads_id}] Переход на Warpcast выполнен (предположительно).")
                    time.sleep(5)
                    # ВАЖНО: Убедитесь, что warpcast НЕ ожидает аргумент pause_range
                    warpcast(driver,
                             config_data['text_file'],
                             config_data['comment_file'],
                             locks['text_lock'],
                             locks['comment_lock'],
                             enabled_actions=warpcast_actions_to_run,
                             picture_folder_path=config_data['picture_folder'],
                             picture_lock=picture_folder_lock)
                    completion_pause = random.uniform(pause_after_min, pause_after_max)
                    logging.info(f"[{ads_id}] Работа с Warpcast завершена. Пауза {completion_pause:.1f} сек...")
                    time.sleep(completion_pause)
                else:
                    logging.info(f"[{ads_id}] Пропуск задач Warpcast (режим: {run_mode}).")

                # --- Запуск Towns ---
                if run_mode in ["towns", "both"]:
                    logging.info(f"[{ads_id}] Запуск задач Towns (режим: {run_mode})...")
                    driver.get("https://app.towns.com/")
                    time.sleep(10)
                    logging.info(f"[{ads_id}] Переход на Towns выполнен (предположительно).")
                    # ВАЖНО: Убедитесь, что towns НЕ ожидает аргумент pause_range
                    towns(driver,
                          config_data['text_gen'],
                          enabled_actions=towns_actions_to_run)
                    completion_pause = random.uniform(pause_after_min, pause_after_max)
                    logging.info(f"[{ads_id}] Работа с Towns завершена. Пауза {completion_pause:.1f} сек...")
                    time.sleep(completion_pause)
                else:
                     logging.info(f"[{ads_id}] Пропуск задач Towns (режим: {run_mode}).")

                logging.info(f"[{ads_id}] Все выбранные задачи успешно завершены.")

            except WebDriverException as task_wd_err:
                 logging.error(f"[{ads_id}] Ошибка WebDriver: {type(task_wd_err).__name__} - {task_wd_err}", exc_info=True)
            except Exception as task_err:
                 logging.error(f"[{ads_id}] Непредвиденная ошибка: {type(task_err).__name__} - {task_err}", exc_info=True)

    except Exception as general_err:
        logging.error(f"[{ads_id}] Общая критическая ошибка: {type(general_err).__name__} - {general_err}", exc_info=True)

    finally:
        # --- Закрытие браузера и сессии AdsPower ---
        if driver:
            try:
                logging.info(f"[{ads_id}] Попытка закрыть WebDriver...")
                driver.quit()
                logging.info(f"[{ads_id}] WebDriver закрыт.")
            except WebDriverException as quit_err:
                logging.error(f"[{ads_id}] Ошибка при закрытии WebDriver: {quit_err}")
            except Exception as final_driver_err:
                logging.error(f"[{ads_id}] Неожиданная ошибка при driver.quit(): {final_driver_err}")

        if session_started:
            close_url = f"http://local.adspower.net:50325/api/v1/browser/stop?user_id={ads_id}"
            try:
                logging.info(f"[{ads_id}] Попытка закрыть сессию AdsPower через API...")
                requests.get(close_url, timeout=30)
                logging.info(f"[{ads_id}] Запрос на закрытие сессии AdsPower отправлен.")
            except requests.exceptions.RequestException as close_api_err:
                logging.error(f"[{ads_id}] Ошибка API AdsPower при закрытии сессии: {close_api_err}")
            except Exception as final_close_err:
                logging.error(f"[{ads_id}] Неожиданная ошибка при закрытии сессии AdsPower: {final_close_err}")

        logging.info(f"Завершение работы потока, который работал с ID: {ads_id}")


# --- Запуск потоков ---
if __name__ == "__main__":
    logging.info("=" * 30)
    logging.info(f"Запуск скрипта автоматизации.")

    # Загрузка конфигурации
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
        ADS_ID_FILE = config.get("ads_id_file", "ADSid.txt")
        MAX_WORKERS = config.get("max_workers", 1)
        RUN_MODE = config.get("run_mode", "both").lower()

        NUM_PROFILES_TO_SELECT = config.get("num_profiles_to_select", 50)

        if RUN_MODE not in ["warpcast", "towns", "both"]:
             logging.warning(f"Некорректный run_mode '{RUN_MODE}'. Используется 'both'.")
             RUN_MODE = "both"

        # --- Загрузка списков разрешенных действий ---
        DEFAULT_WARPCAST_ACTIONS = ["cast", "follow_followers", "run_multiple_interactions", "follow_new_followers", "delete_post", "likes"]
        DEFAULT_TOWNS_ACTIONS = ["choice_town", "text", "scroll_town", "bober"]
        WARPCAST_ENABLED_ACTIONS = config.get("warpcast_enabled_actions", DEFAULT_WARPCAST_ACTIONS)
        TOWNS_ENABLED_ACTIONS = config.get("towns_enabled_actions", DEFAULT_TOWNS_ACTIONS)
        # --------------------------------------------
        logging.info(f"Файл ID: {ADS_ID_FILE}, Потоков: {MAX_WORKERS}, Режим: {RUN_MODE}")
        logging.info(f"Warpcast Действия: {WARPCAST_ENABLED_ACTIONS}")
        logging.info(f"Towns Действия: {TOWNS_ENABLED_ACTIONS}")
        logging.info("=" * 30)
    except Exception as cfg_err: # Упрощенная обработка
         logging.error(f"Критическая ошибка загрузки config.json: {cfg_err}", exc_info=True); exit()

    # Загрузка ID профилей
    all_available_ads_ids = load_ads_ids(ADS_ID_FILE)

    if not all_available_ads_ids:
        logging.error("Нет доступных ID профилей в файле для запуска. Завершение работы.")
        exit()

    logging.info(f"Всего доступно ID в файле: {len(all_available_ads_ids)}")

    # --- НОВАЯ ЛОГИКА: Случайная выборка N профилей ---
    if len(all_available_ads_ids) > NUM_PROFILES_TO_SELECT:
        logging.info(f"Выбираем случайно {NUM_PROFILES_TO_SELECT} профилей из {len(all_available_ads_ids)}...")
        selected_ads_ids = random.sample(all_available_ads_ids, NUM_PROFILES_TO_SELECT)
        logging.info(f"Выбрано {len(selected_ads_ids)} профилей для работы.")
    else:
        logging.info(
            f"Количество доступных профилей ({len(all_available_ads_ids)}) меньше или равно запрошенному ({NUM_PROFILES_TO_SELECT}). Используем все доступные.")
        selected_ads_ids = list(all_available_ads_ids)  # Просто берем все, list() для копии

    if not selected_ads_ids:  # Дополнительная проверка, хотя не должна случиться если all_available_ads_ids не пуст
        logging.error("После выборки не осталось ID для работы. Завершение.")
        exit()
    # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

    # Перемешиваем уже ВЫБРАННЫЙ список ID для случайного порядка обработки потоками
    random.shuffle(selected_ads_ids)
    logging.info(
        f"Итоговый список ID для обработки ({len(selected_ads_ids)} шт.): {selected_ads_ids if len(selected_ads_ids) < 10 else str(selected_ads_ids[:5]) + '... и др.'}")

    # Подготовка общих данных для потоков
    config_data_for_thread = {
        'profile_start_delay_range': config.get("profile_start_delay_range", [0, 0]),
        'run_mode': RUN_MODE,
        'project_completion_pause_range': config.get("project_completion_pause_range", [60, 120]),
        'text_file': config.get("text_file", "text.txt"),
        'comment_file': config.get("comment_file", "comment.txt"),
        'picture_folder': config.get("picture_folder", "picture"),
        'text_gen': {
            'api_key': config.get("openrouter_api_key"),
            'model': config.get("openrouter_model", "mistralai/mistral-7b-instruct"),
            'system_prompt': config.get("openrouter_system_prompt", "Ты пишешь короткие описания...")},
        'warpcast_enabled_actions': WARPCAST_ENABLED_ACTIONS, # Передаем список
        'towns_enabled_actions': TOWNS_ENABLED_ACTIONS    # Передаем список
    }
    file_locks = { 'text_lock': text_file_lock, 'comment_lock': comment_file_lock, 'picture_lock': picture_folder_lock }

    # ИЗМЕНЕНО: Используем executor.map снова, т.к. worker_index не нужен
    logging.info(f"Запуск обработки для {len(selected_ads_ids)} профилей с {MAX_WORKERS} потоками...")
    if selected_ads_ids:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # Lambda принимает только ads_id, остальные аргументы фиксированы
            task_function = lambda current_ads_id: run_profile_tasks(
                ads_id=current_ads_id,
                config_data=config_data_for_thread,
                locks=file_locks
                # worker_index и total_workers больше не передаются
            )
            # map применяет task_function к каждому ID
            results = list(executor.map(task_function, selected_ads_ids))
    else:
        logging.info("Нет задач для запуска.")

    logging.info("=" * 30)
    logging.info("Все потоки завершили свою работу.")
    logging.info("Скрипт завершен.")
    logging.info("=" * 30)