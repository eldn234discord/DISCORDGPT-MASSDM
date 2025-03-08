############################################################
# SEGMENT 1: IMPORTS, CONFIG, AND COMMON HELPERS
############################################################

import os
import sys
import time
import random
import logging
import requests
from pathlib import Path

import undetected_chromedriver as uc
from dotenv import load_dotenv

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
    MoveTargetOutOfBoundsException
)

from urllib.parse import urlparse, parse_qs
import openai

# Setup basic logging
logger = logging.getLogger("after_join_with_two_checkbox_clicks")
logger.setLevel(logging.INFO)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
logger.addHandler(ch)

logger.info("=== SCRIPT START (2x checkbox clicks) ===")

# Load environment variables
load_dotenv()

# Configuration
# Windows-only user agents, as requested
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; WOW64; rv:55.0) Gecko/20100101 Firefox/55.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
]

USER_DATA_DIR = os.getenv("CHROME_USER_DATA_DIR", "")
TOKENS_POOL_PATH = os.getenv("TOKENS_POOL_PATH", "tokens_pool.txt")

JOIN_THROTTLE_FILE = "last_join_time.txt"
JOIN_MIN_INTERVAL = 180
MAX_POOL_SIZE = 500

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "sk-...YOUR_GPT_KEY...")


############################################################
# HELPER FUNCTIONS
############################################################

def snippet_for_token(token: str) -> str:
    """
    Inject token using a JavaScript snippet, which sets localStorage.token 
    and then reloads the page to log in.
    """
    return f"""
    function login(token) {{
      setInterval(() => {{
        document.body.appendChild(document.createElement('iframe'))
          .contentWindow.localStorage.token = `\"{token}\"`;
      }}, 50);
      setTimeout(() => {{
        location.reload();
      }}, 2500);
    }}
    login('{token}');
    """

def random_delay(min_s=2, max_s=6):
    time.sleep(random.uniform(min_s, max_s))

def random_mouse_move(driver, times=2, arc=True):
    """Random mouse movements with ActionChains to simulate human moves."""
    act = ActionChains(driver)
    w = driver.execute_script("return window.innerWidth;") or 800
    h = driver.execute_script("return window.innerHeight;") or 600
    safe_w = int(w * 0.8)
    safe_h = int(h * 0.8)

    for _ in range(times):
        sx = random.randint(0, safe_w)
        sy = random.randint(0, safe_h)
        try:
            act.move_by_offset(sx, sy).perform()
            time.sleep(random.uniform(0.2, 0.5))

            if arc:
                mx = random.randint(0, safe_w)
                my = random.randint(0, safe_h)
                act.move_by_offset(mx - sx, my - sy).perform()
                time.sleep(random.uniform(0.2, 0.5))

            act.move_by_offset(-sx, -sy).perform()
            time.sleep(random.uniform(0.2, 0.5))
        except MoveTargetOutOfBoundsException:
            logger.info("[random_mouse_move] => out of bounds => skip.")
            pass

def type_with_typos_and_corrections(elem, text: str,
                                    delay=(0.02, 0.06),
                                    max_typos=2,
                                    typo_chance=0.12):
    """
    Type text into 'elem' with random typos and corrections 
    to appear more human-like.
    """
    possible_typos = random.randint(0, max_typos)
    length = len(text)
    pause_index = None
    if length > 5 and random.random() < 0.3:
        pause_index = random.randint(2, length - 2)

    for i, c in enumerate(text):
        # Possibly introduce a random typo
        if possible_typos > 0 and random.random() < typo_chance:
            wrong_char = random.choice("abcdefghijklmnopqrstuvwxyz0123456789")
            elem.send_keys(wrong_char)
            time.sleep(random.uniform(*delay))
            elem.send_keys(Keys.BACKSPACE)
            possible_typos -= 1
            time.sleep(random.uniform(*delay))

        elem.send_keys(c)
        time.sleep(random.uniform(*delay))

        # random mid-typing pause
        if pause_index and i == pause_index:
            time.sleep(random.uniform(1, 3))

def dm_delay():
    """Delay after sending a DM, to appear less spammy."""
    time.sleep(30)

def remove_overlay(driver):
    """If a hCaptcha overlay is present, lower its z-index to allow clicking."""
    try:
        overlays = driver.find_elements(By.CSS_SELECTOR, "div[style*='opacity: 0.05']")
        for ov in overlays:
            driver.execute_script("arguments[0].style.zIndex='-999999';", ov)
            logger.info("[remove_overlay] => lowered z-index for overlay.")
    except Exception as e:
        logger.info(f"[remove_overlay] => error => {e}")

def human_like_click(driver, element):
    """
    ActionChains click => do not use direct .click().
    This ensures all clickable interactions are action-chains-based only.
    """
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
    except:
        pass
    actions = ActionChains(driver)
    actions.move_to_element(element).pause(random.uniform(0.2, 0.7)).click().perform()
    time.sleep(random.uniform(0.3, 0.7))


############################################################
# DETECTING / SOLVING HCAPTCHA
############################################################

def puzzle_iframe_exists(driver) -> bool:
    frames = driver.find_elements(By.TAG_NAME, "iframe")
    for f_ in frames:
        src_ = f_.get_attribute("src") or ""
        if "hcaptcha.com" in src_ and "frame=challenge" in src_:
            return True
    return False

def open_text_challenge_flow(driver):
    """
    Attempt to open the text challenge for hCaptcha using ActionChains clicks.
    If puzzle not loaded => re-click checkbox.
    """
    logger.info("[open_text_challenge_flow] => start => first checkbox click")

    # 1) find & click #checkbox
    frames_checkbox = driver.find_elements(By.TAG_NAME, "iframe")
    for fcb in frames_checkbox:
        src_ = fcb.get_attribute("src") or ""
        if "hcaptcha.com" in src_ and "frame=checkbox" in src_:
            driver.switch_to.default_content()
            driver.switch_to.frame(fcb)
            remove_overlay(driver)
            try:
                cb_el = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.ID, "checkbox"))
                )
                human_like_click(driver, cb_el)
                logger.info("[open_text_challenge_flow] => ActionChains => clicked #checkbox")
                time.sleep(2)
            except Exception as e:
                logger.info(f"[open_text_challenge_flow] => cannot click => {e}")
            driver.switch_to.default_content()

    # 2) #menu-info => #text_challenge
    frames_menu = driver.find_elements(By.TAG_NAME, "iframe")
    for fm in frames_menu:
        s_ = fm.get_attribute("src") or ""
        if "hcaptcha.com" in s_:
            driver.switch_to.default_content()
            driver.switch_to.frame(fm)
            remove_overlay(driver)
            try:
                menu_el = driver.find_element(By.ID, "menu-info")
                human_like_click(driver, menu_el)
                logger.info("[open_text_challenge_flow] => ActionChains => clicked #menu-info")
                time.sleep(2)

                text_ch = driver.find_element(By.ID, "text_challenge")
                human_like_click(driver, text_ch)
                logger.info("[open_text_challenge_flow] => ActionChains => clicked #text_challenge")
                time.sleep(2)
            except Exception as e:
                logger.info(f"[open_text_challenge_flow] => skip => {e}")
            driver.switch_to.default_content()

    # If puzzle didn't load => re-click checkbox
    if not puzzle_iframe_exists(driver):
        logger.info("[open_text_challenge_flow] => puzzle not loaded => re-click #checkbox..")
        frames_checkbox_again = driver.find_elements(By.TAG_NAME, "iframe")
        for f2 in frames_checkbox_again:
            src_3 = f2.get_attribute("src") or ""
            if "hcaptcha.com" in src_3 and "frame=checkbox" in src_3:
                driver.switch_to.default_content()
                driver.switch_to.frame(f2)
                remove_overlay(driver)
                try:
                    cb_re = WebDriverWait(driver, 3).until(
                        EC.element_to_be_clickable((By.ID, "checkbox"))
                    )
                    human_like_click(driver, cb_re)
                    logger.info("[open_text_challenge_flow] => re-clicked #checkbox after text_ch attempt.")
                    time.sleep(2)
                except Exception as e2:
                    logger.info(f"[open_text_challenge_flow] => second re-click => {e2}")
                driver.switch_to.default_content()

    logger.info("[open_text_challenge_flow] => done opening text challenge.")


def call_gpt4_mini_api(question: str, text_block: str, openai_key: str) -> str:
    """
    Call GPT-4 to solve a short text puzzle question.
    """
    logger.info("[call_gpt4_mini_api] => start GPT call..")
    openai.api_key = openai_key

    prompt_msg = (
        f"Puzzle question: {question}\n"
        f"Additional text:\n{text_block}\n\n"
        "Provide a short, direct answer (single word, number, or phrase):"
    )

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are a puzzle-solving assistant."},
                {"role": "user", "content": prompt_msg},
            ],
            max_tokens=500,
            temperature=0.6
        )
        ans = response.choices[0].message["content"].strip()
        logger.info(f"[call_gpt4_mini_api] => GPT-4 answer => {ans}")
        return ans
    except Exception as e:
        logger.info(f"[call_gpt4_mini_api] => error => {e}")
        return ""


def solve_text_challenge_join(driver):
    """
    Puzzle solver used in "join server" flow.
    Up to 3 puzzle steps total, returning to default content after each step.
    """
    logger.info("[solve_text_challenge_join] => up to 3 steps puzzle solver (join).")

    # Possibly re-click the checkbox if puzzle not loaded
    if not puzzle_iframe_exists(driver):
        logger.info("[solve_text_challenge_join] => puzzle not loaded => re-click #checkbox..")
        frames_checkbox2 = driver.find_elements(By.TAG_NAME, "iframe")
        for fcb2 in frames_checkbox2:
            sc2_ = fcb2.get_attribute("src") or ""
            if "hcaptcha.com" in sc2_ and "frame=checkbox" in sc2_:
                driver.switch_to.default_content()
                driver.switch_to.frame(fcb2)
                remove_overlay(driver)
                try:
                    cb_2 = WebDriverWait(driver, 3).until(
                        EC.element_to_be_clickable((By.ID, "checkbox"))
                    )
                    human_like_click(driver, cb_2)
                    logger.info("[solve_text_challenge_join] => re-clicked #checkbox => puzzle not loaded.")
                    time.sleep(2)
                except Exception as e:
                    logger.info(f"[solve_text_challenge_join] => skip => {e}")
                driver.switch_to.default_content()

        if not puzzle_iframe_exists(driver):
            logger.info("[solve_text_challenge_join] => puzzle STILL not loaded => skip puzzle flow.")
            return

    max_steps = 3
    step_num = 0
    while step_num < max_steps:
        step_num += 1
        logger.info(f"[solve_text_challenge_join] => step {step_num}/{max_steps} => searching puzzle iframe..")

        puzzle_iframe = None
        frames = driver.find_elements(By.TAG_NAME, "iframe")
        for f_ in frames:
            s_ = f_.get_attribute("src") or ""
            if "hcaptcha.com" in s_ and "frame=challenge" in s_:
                puzzle_iframe = f_
                break

        if not puzzle_iframe:
            logger.info("[solve_text_challenge_join] => no #frame=challenge => puzzle done => break.")
            break

        try:
            driver.switch_to.frame(puzzle_iframe)
            logger.info(f"[solve_text_challenge_join] => in puzzle iframe => step {step_num}")
        except Exception as e:
            logger.info(f"[solve_text_challenge_join] => can't switch => {e}")
            driver.switch_to.default_content()
            break

        puzzle_question = ""
        puzzle_text_block = ""
        try:
            prompt_el = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#prompt"))
            )
            puzzle_question = prompt_el.text.strip()
        except Exception as e:
            logger.info(f"[solve_text_challenge_join] => no #prompt => {e}")
            driver.switch_to.default_content()
            break

        try:
            text_el = driver.find_element(By.CSS_SELECTOR, ".challenge-text")
            puzzle_text_block = text_el.text.strip()
        except Exception as e:
            logger.info(f"[solve_text_challenge_join] => no .challenge-text => {e}")

        logger.info(f"[solve_text_challenge_join] => question => {puzzle_question}")
        logger.info(f"[solve_text_challenge_join] => text_block => {puzzle_text_block}")

        ans = call_gpt4_mini_api(puzzle_question, puzzle_text_block, OPENAI_API_KEY)
        if not ans:
            logger.info("[solve_text_challenge_join] => GPT empty => break.")
            driver.switch_to.default_content()
            break

        # Type the GPT answer
        try:
            input_box = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='text']"))
            )
            input_box.clear()
            type_with_typos_and_corrections(input_box, ans)
            time.sleep(1)

            for attempt in range(5):
                try:
                    next_btn = WebDriverWait(driver, 3).until(
                        EC.element_to_be_clickable((
                            By.CSS_SELECTOR,
                            "body > div > div.interface-challenge > div.button-submit.button"
                        ))
                    )
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", next_btn)
                    time.sleep(0.5)
                    human_like_click(driver, next_btn)
                    logger.info(f"[solve_text_challenge_join] => typed => {ans}, clicked Next/Submit.")
                    time.sleep(3)
                    break
                except (TimeoutException, StaleElementReferenceException, WebDriverException) as e:
                    logger.info(
                        f"[solve_text_challenge_join] => Next/Submit not clickable => attempt #{attempt+1} => {e}"
                    )
                    driver.execute_script("window.scrollBy(0, 50);")
                    time.sleep(1)
        except Exception as e:
            logger.info(f"[solve_text_challenge_join] => puzzle error => {e}")
            driver.switch_to.default_content()
            break

        driver.switch_to.default_content()
        logger.info("[solve_text_challenge_join] => puzzle iteration done.")

    logger.info("[solve_text_challenge_join] => normal puzzle flow done.")


def solve_text_challenge_dm(driver):
    """
    DM puzzle solver that tries a maximum of 3 times (3 sets). 
    Each set has up to 3 puzzles. 
    So total max 3*3=9 puzzle steps for DM captcha.

    1) Switch into the puzzle iFrame once, remain there.
    2) For each set (up to 3 sets):
       - Attempt up to 3 puzzle steps. 
       - If after those steps we see "Please try again." => we do next set.
       - If puzzle disappears or we see success => break.
    3) If we used all 3 sets and still "Please try again." => we exit puzzle.
    """

    logger.info("[solve_text_challenge_dm] => DM puzzle => up to 3 sets of 3 attempts each => max 9 puzzle steps.")

    # Possibly re-click #checkbox if puzzle not loaded
    if not puzzle_iframe_exists(driver):
        logger.info("[solve_text_challenge_dm] => puzzle not loaded => re-click #checkbox..")
        frames_checkbox2 = driver.find_elements(By.TAG_NAME, "iframe")
        for fcb2 in frames_checkbox2:
            sc2_ = fcb2.get_attribute("src") or ""
            if "hcaptcha.com" in sc2_ and "frame=checkbox" in sc2_:
                driver.switch_to.default_content()
                driver.switch_to.frame(fcb2)
                remove_overlay(driver)
                try:
                    cb_2 = WebDriverWait(driver, 3).until(
                        EC.element_to_be_clickable((By.ID, "checkbox"))
                    )
                    human_like_click(driver, cb_2)
                    logger.info("[solve_text_challenge_dm] => re-clicked #checkbox => puzzle not loaded.")
                    time.sleep(2)
                except Exception as e:
                    logger.info(f"[solve_text_challenge_dm] => skip => {e}")
                driver.switch_to.default_content()

        if not puzzle_iframe_exists(driver):
            logger.info("[solve_text_challenge_dm] => puzzle STILL not loaded => skip puzzle flow.")
            return

    # Switch into puzzle iFrame (once)
    puzzle_iframe = None
    frames_ = driver.find_elements(By.TAG_NAME, "iframe")
    for f_ in frames_:
        s_ = f_.get_attribute("src") or ""
        if "hcaptcha.com" in s_ and "frame=challenge" in s_:
            puzzle_iframe = f_
            break

    if not puzzle_iframe:
        logger.info("[solve_text_challenge_dm] => cannot find puzzle iFrame => skip.")
        return

    # Try to switch in
    try:
        driver.switch_to.default_content()
        driver.switch_to.frame(puzzle_iframe)
        logger.info("[solve_text_challenge_dm] => switched into puzzle iFrame => up to 3 sets..")
    except Exception as e:
        logger.info(f"[solve_text_challenge_dm] => cannot switch => {e}")
        return

    max_sets = 3  # we do at most 3 sets
    steps_per_set = 3
    current_set = 0

    while current_set < max_sets:
        current_set += 1
        logger.info(f"[solve_text_challenge_dm] => starting set {current_set}/{max_sets} => up to {steps_per_set} steps in this set.")

        # Attempt up to 3 puzzle steps in this set
        step_num = 0
        while step_num < steps_per_set:
            step_num += 1
            logger.info(f"[solve_text_challenge_dm] => set={current_set}, step={step_num}/{steps_per_set}")

            # 1) see if puzzle is still there (#prompt)
            try:
                prompt_el = WebDriverWait(driver, 4).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "#prompt"))
                )
                puzzle_question = prompt_el.text.strip()
            except Exception as e:
                logger.info("[solve_text_challenge_dm] => #prompt not found => puzzle might be solved => stop.")
                return

            puzzle_text_block = ""
            try:
                text_el = driver.find_element(By.CSS_SELECTOR, ".challenge-text")
                puzzle_text_block = text_el.text.strip()
            except Exception as e:
                logger.info(f"[solve_text_challenge_dm] => no .challenge-text => {e}")

            logger.info(f"[solve_text_challenge_dm] => question => {puzzle_question}")
            logger.info(f"[solve_text_challenge_dm] => text_block => {puzzle_text_block}")

            ans = call_gpt4_mini_api(puzzle_question, puzzle_text_block, OPENAI_API_KEY)
            if not ans:
                logger.info("[solve_text_challenge_dm] => GPT answer empty => break puzzle.")
                return

            try:
                input_box = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='text']"))
                )
                input_box.clear()
                type_with_typos_and_corrections(input_box, ans)
                time.sleep(1)

                # Next/Submit with action-chains
                submit_clicked = False
                for attempt2 in range(5):
                    try:
                        next_btn = WebDriverWait(driver, 3).until(
                            EC.element_to_be_clickable((
                                By.CSS_SELECTOR,
                                "body > div > div.interface-challenge > div.button-submit.button"
                            ))
                        )
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", next_btn)
                        time.sleep(0.5)
                        human_like_click(driver, next_btn)  # action-chains
                        logger.info(f"[solve_text_challenge_dm] => typed => {ans}, clicked Next/Submit.")
                        time.sleep(3)
                        submit_clicked = True
                        break
                    except (TimeoutException, StaleElementReferenceException, WebDriverException) as e:
                        logger.info(
                            f"[solve_text_challenge_dm] => Next/Submit not clickable => attempt #{attempt2+1} => {e}"
                        )
                        driver.execute_script("window.scrollBy(0, 50);")
                        time.sleep(1)

                if not submit_clicked:
                    logger.warning("[solve_text_challenge_dm] => Could not click Next/Submit after 5 attempts.")

            except Exception as e:
                logger.info(f"[solve_text_challenge_dm] => puzzle step error => {e}")
                return

            logger.info("[solve_text_challenge_dm] => step done => check if 'Please try again.'..")
            time.sleep(2)

            # check if puzzle is successful or not
            # We'll do a quick read of puzzle's page_source
            # If the puzzle iFrame is gone => success
            # else => see if "Please try again." is in the iFrame
            try:
                # Check if #prompt still present
                # If not present => puzzle presumably solved
                test_prompt = driver.find_element(By.CSS_SELECTOR, "#prompt")
                # #prompt is present => let's see if "Please try again." or "unsuccessful"
                ps = driver.page_source.lower()
                if ("please try again." in ps) or ("unsuccessful" in ps):
                    logger.info("[solve_text_challenge_dm] => unsuccessful => continue steps in this set..")
                else:
                    logger.info("[solve_text_challenge_dm] => puzzle presumably solved => exit early.")
                    return

            except NoSuchElementException:
                logger.info("[solve_text_challenge_dm] => #prompt not found => puzzle iFrame gone => success => exit.")
                return

        # If we finish the set's 3 steps => check if puzzle is still unsolved
        # Possibly "Please try again."
        try:
            # If #prompt is gone => success
            final_prompt = driver.find_element(By.CSS_SELECTOR, "#prompt")
            ps2 = driver.page_source.lower()
            if ("please try again." in ps2) or ("unsuccessful" in ps2):
                logger.info(f"[solve_text_challenge_dm] => set {current_set} done => still 'Please try again.' => next set..")
                # move on to the next set
            else:
                logger.info(f"[solve_text_challenge_dm] => set {current_set} done => puzzle presumably solved => exit.")
                return
        except NoSuchElementException:
            logger.info("[solve_text_challenge_dm] => puzzle iFrame gone => success => exit.")
            return

    logger.info("[solve_text_challenge_dm] => All 3 sets (3x3=9 steps) used => puzzle remains => exit puzzle.")


############################################################
# UTILS & RANDOM ACTIONS
############################################################

def fallback_slate_span(driver, message):
    try:
        spans = driver.find_elements(By.XPATH, "//span[@data-slate-string='true']")
        if not spans:
            return False
        sp_ = spans[0]
        sp_.location_once_scrolled_into_view
        time.sleep(0.2)
        human_like_click(driver, sp_)
        time.sleep(0.5)
        type_with_typos_and_corrections(sp_, message)
        time.sleep(1)
        sp_.send_keys(Keys.ENTER)
        return True
    except:
        return False

def fallback_textarea(driver, message):
    try:
        txels = driver.find_elements(By.XPATH, "//textarea[contains(@placeholder,'Message')]")
        if not txels:
            return False
        ar_ = txels[0]
        ar_.location_once_scrolled_into_view
        time.sleep(0.3)
        human_like_click(driver, ar_)
        time.sleep(0.3)
        type_with_typos_and_corrections(ar_, message)
        time.sleep(0.5)
        ar_.send_keys(Keys.ENTER)
        return True
    except:
        return False

def paste_message_in_textarea(driver, message):
    success_main = False
    for _ in range(2):
        try:
            main_ = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH,
                    "//div[@role='textbox' and contains(@class,'slateTextArea_') "
                    "and contains(@class,'textAreaForUserProfile_')]"
                ))
            )
            human_like_click(driver, main_)
            time.sleep(0.5)
            type_with_typos_and_corrections(main_, message)
            time.sleep(random.uniform(1, 3))
            main_.send_keys(Keys.ENTER)
            logger.info("[paste_message_in_textarea] => typed => success.")
            success_main = True
            break
        except (TimeoutException, StaleElementReferenceException):
            time.sleep(1)

    if not success_main:
        if fallback_slate_span(driver, message):
            logger.info("[paste_message_in_textarea] => fallback_slate_span => success.")
            return
        if fallback_textarea(driver, message):
            logger.info("[paste_message_in_textarea] => fallback_textarea => success.")
            return
        logger.info("[paste_message_in_textarea] => all fallbacks => no success.")

def random_small_actions(driver):
    try:
        if random.random() < 0.2:
            idle_time = random.uniform(5, 10)
            logger.info(f"[random_small_actions] => idle => {idle_time:.2f}s")
            time.sleep(idle_time)

        servers = driver.find_elements(By.CSS_SELECTOR, "li[data-list-item-id^='guildsnav_']")
        if servers and random.random() < 0.3:
            c_ = random.choice(servers)
            c_.location_once_scrolled_into_view
            human_like_click(driver, c_)
            logger.info("[random_small_actions] => clicked random server.")
            time.sleep(random.uniform(2, 4))
            driver.back()
            time.sleep(random.uniform(1, 2))
    except:
        pass

def pinned_messages_interaction(driver):
    try:
        pinned_btn = driver.find_element(By.XPATH, "//button[@aria-label='Pinned Messages']")
        human_like_click(driver, pinned_btn)
        time.sleep(1)
        b_ = driver.find_element(By.TAG_NAME, "body")
        b_.send_keys(Keys.ESCAPE)
    except:
        pass

def random_channel_switch(driver):
    try:
        c_els = driver.find_elements(By.XPATH,
            "//div[contains(@aria-label,'Channels')]//a[contains(@href,'/channels/')]"
        )
        if c_els:
            rc = random.choice(c_els)
            rc.location_once_scrolled_into_view
            human_like_click(driver, rc)
            logger.info("[random_channel_switch] => switched random channel.")
            time.sleep(1)
            driver.back()
    except:
        pass

def ensure_member_list_open(driver):
    try:
        hide_btn = driver.find_element(By.XPATH,
            "//*[contains(@aria-label,'Hide Member List') and "
            "(@role='button' or @type='button')]"
        )
        if hide_btn:
            return
    except:
        pass
    try:
        show_btn = driver.find_element(By.XPATH,
            "//*[contains(@aria-label,'Show Member List') and "
            "(@role='button' or @type='button')]"
        )
        human_like_click(driver, show_btn)
        time.sleep(1)
    except:
        pass

def verify_dm_sent(driver, content, wait_secs=5):
    end_t = time.time() + wait_secs
    while time.time() < end_t:
        msgs = driver.find_elements(By.XPATH,
            "//div[contains(@class,'messageContent_') and contains(text(),'')]"
        )
        if msgs and content.strip() in msgs[-1].text.strip():
            return True
        time.sleep(1)
    return False

def click_add_server_button(driver, timeout=15):
    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.HOME)
    time.sleep(2)
    try:
        add_icon = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH,
                "//div[@aria-label='Add a Server' and contains(@class,'circleIconButton_')]"
            ))
        )
        human_like_click(driver, add_icon)
        logger.info("[click_add_server_button] => normal 'Add a Server' (XPATH).")
        return True
    except Exception as e:
        logger.info(f"[click_add_server_button] => normal approach fail => {e}")

    try:
        fallback_btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR,
                "li[data-list-item-id='create-guild-button']"
            ))
        )
        human_like_click(driver, fallback_btn)
        logger.info("[click_add_server_button] => fallback 'create-guild-button' (CSS).")
        return True
    except Exception as e2:
        logger.info(f"[click_add_server_button] => fallback approach fail => {e2}")

    return False


############################################################
# DISCORDSERVERDM CLASS
############################################################

class DiscordServerDM:
    """
    - join_server => puzzle with solve_text_challenge_join (3 steps).
    - partial_scrape_and_dm => puzzle with solve_text_challenge_dm 
      (3 sets x 3 steps = 9 total puzzle attempts).
    - All clickable interactions use 'human_like_click()' 
      which is ActionChains-based only.
    """
    def __init__(self, token):
        self.token = token
        self.driver = None
        self.seen = set()
        self.user_msg = ""
        self.server_id = ""
        self.channel_id = ""
        self.channel_url = ""
        self.current_pass = 0
        self.locked = False
        self.consecutive_fail_passes = 0

    def setup_driver(self, headless=False):
        opts = uc.ChromeOptions()
        ua = random.choice(USER_AGENTS)
        opts.add_argument(f"--user-agent={ua}")
        opts.add_argument("--lang=en-US")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--window-size=1920,1080")

        if USER_DATA_DIR:
            logger.info(f"[setup_driver] => Using user-data-dir => {USER_DATA_DIR}")
            opts.add_argument(f"--user-data-dir={USER_DATA_DIR}")

        if headless:
            opts.add_argument("--headless")

        self.driver = uc.Chrome(options=opts, version_main=132)
        time.sleep(2)
        try:
            self.driver.maximize_window()
        except:
            pass

    def token_login(self):
        logger.info(f"[token_login] => start token => {self.token[:10]}..")
        d = self.driver
        d.get("https://discord.com/login")
        time.sleep(4)
        logger.info("[token_login] => injecting token..")
        d.execute_script(snippet_for_token(self.token))
        time.sleep(8)

        random_mouse_move(d, times=random.randint(2, 5), arc=True)
        for _ in range(30):
            if "channels/@me" in d.current_url:
                logger.info("[token_login] => success => /channels/@me => proceed.")
                self.simulate_normal_usage()
                return True
            time.sleep(1)
        logger.warning("[token_login] => not on /channels/@me => fail => possibly locked.")
        return False

    def check_locked(self):
        """If we land on /login, the token is locked."""
        if self.driver.current_url.startswith("https://discord.com/login"):
            logger.info("[check_locked] => locked => /login => token locked.")
            self.locked = True

    def simulate_normal_usage(self):
        logger.info("[simulate_normal_usage] => random usage..")
        random_small_actions(self.driver)
        pinned_messages_interaction(self.driver)
        random_channel_switch(self.driver)
        random_delay(3, 8)

    def fetch_guild_and_channel(self, invite_url):
        cd_ = invite_url.rstrip("/").split("/")[-1]
        logger.info(f"[fetch_guild_and_channel] => code => {cd_}")
        if not cd_:
            return None, None
        link = f"https://discord.com/api/v9/invites/{cd_}"
        try:
            r_ = requests.get(link, timeout=10)
            logger.info(f"[fetch_guild_and_channel] => GET => status={r_.status_code}")
            if r_.status_code == 200:
                j_ = r_.json()
                gid = j_.get("guild", {}).get("id")
                cid = j_.get("channel", {}).get("id")
                logger.info(f"[fetch_guild_and_channel] => guild={gid}, channel={cid}")
                if gid and cid:
                    return gid, cid
        except Exception as e:
            logger.info(f"[fetch_guild_and_channel] => error => {e}")
        return None, None

    def join_server_throttle_check(self):
        now = time.time()
        last_time = 0
        if os.path.exists(JOIN_THROTTLE_FILE):
            with open(JOIN_THROTTLE_FILE, "r", encoding="utf-8") as f:
                try:
                    last_time = float(f.read().strip())
                except:
                    last_time = 0
        diff = now - last_time
        if diff < JOIN_MIN_INTERVAL:
            wait_s = JOIN_MIN_INTERVAL - diff
            logger.info(f"[join_server_throttle_check] => waiting {wait_s:.1f}s..")
            time.sleep(wait_s)
        with open(JOIN_THROTTLE_FILE, "w", encoding="utf-8") as f2:
            f2.write(str(time.time()))

    def join_server(self, invite_url: str) -> bool:
        """
        Attempt to join a server, using solve_text_challenge_join
        if puzzle appears. 
        Only 3 puzzle steps per attempt. If puzzle reappears, 
        we do it again up to 2 total tries.
        """
        logger.info(f"[join_server] => start => invite_url={invite_url}")
        self.join_server_throttle_check()

        d = self.driver
        old_ = d.find_elements(By.CSS_SELECTOR, "li[data-list-item-id^='guildsnav_']")
        old_count = len(old_)
        logger.info(f"[join_server] => old_count => {old_count}")

        random_delay(3, 8)
        if not click_add_server_button(d, timeout=20):
            logger.warning("[join_server] => Could not click 'Add a Server' => abort.")
            return False

        random_delay(2, 5)
        self.check_locked()
        if self.locked:
            return False

        # "Join a Server" button
        try:
            join_btn = WebDriverWait(d, 15).until(
                EC.element_to_be_clickable((By.XPATH,
                    "//button[@type='button' and contains(@class,'footerButton_fc9dae') "
                    "and contains(@class,'button_dd4f85') and contains(.,'Join a Server')]"
                ))
            )
            human_like_click(d, join_btn)
            logger.info("[join_server] => 'Join a Server' => ActionChains click.")
            time.sleep(2)
        except Exception as e:
            logger.info(f"[join_server] => cannot click => {e}")
            return False

        random_delay(2, 5)
        self.check_locked()
        if self.locked:
            return False

        # Type the invite URL
        inv_in = None
        try:
            inv_in = WebDriverWait(d, 8).until(
                EC.presence_of_element_located((By.XPATH,
                    "//input[contains(@class, 'inputDefault_f8bc55') "
                    "and contains(@class, 'input_f8bc55') "
                    "and contains(@class, 'inputInner_e8a9c7')]"
                ))
            )
        except:
            logger.info("[join_server] => STILL no invite input => skip.")
            return False

        if inv_in:
            try:
                human_like_click(d, inv_in)
                time.sleep(0.5)
                inv_in.clear()
                time.sleep(0.5)
                type_with_typos_and_corrections(inv_in, invite_url, max_typos=3, typo_chance=0.25)
                time.sleep(1)
                logger.info("[join_server] => typed invite_url (ActionChains).")
            except Exception as e:
                logger.info(f"[join_server] => trouble typing => {e}")
                return False
        else:
            logger.info("[join_server] => no invite input => fail.")
            return False

        random_delay(2, 5)
        self.check_locked()
        if self.locked:
            return False

        # Final "Join Server"
        try:
            final_join = WebDriverWait(d, 5).until(
                EC.element_to_be_clickable((By.XPATH,
                    "//button[@type='button' and contains(@class,'button_dd4f85') and contains(.,'Join Server')]"
                ))
            )
            human_like_click(d, final_join)
            logger.info("[join_server] => clicked final 'Join Server'")
            time.sleep(3)
        except Exception as e:
            logger.info(f"[join_server] => final => {e}")
            return False

        # Now solve puzzle => normal 3-step approach
        random_delay(2, 5)
        for attempt in range(2):
            self.check_locked()
            if self.locked:
                return False

            open_text_challenge_flow(d)
            solve_text_challenge_join(d)  # up to 3 puzzle steps
            time.sleep(3)
            frames_again = d.find_elements(By.TAG_NAME, "iframe")
            puzzle_frames = [
                f for f in frames_again
                if "hcaptcha.com" in (f.get_attribute("src") or "")
            ]
            if puzzle_frames:
                logger.info("[join_server] => puzzle re-appeared => do one more iteration.")
                time.sleep(2)
                continue
            else:
                logger.info("[join_server] => puzzle did not re-appear => done.")
                break

        time.sleep(5)
        new_ = d.find_elements(By.CSS_SELECTOR, "li[data-list-item-id^='guildsnav_']")
        joined = (len(new_) > old_count)
        logger.info(f"[join_server] => new_count={len(new_)}, joined={joined}")

        if self.server_id:
            c_url = f"https://discord.com/channels/{self.server_id}"
            ccu = d.current_url.strip()
            if ccu.startswith(c_url):
                joined = True

        if not joined:
            logger.info("[join_server] => not joined => proceed anyway.")
        else:
            logger.info("[join_server] => success => joined.")

        return joined

    def partial_scrape_and_dm(self, passes=10, max_users=None):
        """
        For DM. If puzzle => solve_text_challenge_dm => 
        tries up to 3 sets x 3 puzzle steps = 9 attempts.
        """
        d = self.driver
        if not self.server_id or not self.channel_id:
            logger.warning("[partial_scrape_and_dm] => no server/channel => skip.")
            return

        self.channel_url = f"https://discord.com/channels/{self.server_id}/{self.channel_id}"
        logger.info(f"[partial_scrape_and_dm] => goto => {self.channel_url}")
        d.get(self.channel_url)
        time.sleep(6)
        pinned_messages_interaction(d)
        random_channel_switch(d)
        random_mouse_move(d, random.randint(2, 4), arc=True)
        random_small_actions(d)
        ensure_member_list_open(d)

        logger.info(f"[partial_scrape_and_dm] => will do up to {passes} PAGE_DOWN passes (continuing from pass={self.current_pass}).")

        user_count = 0
        while self.current_pass < passes:
            self.check_locked()
            if self.locked:
                logger.info(f"[partial_scrape_and_dm] => locked => stop at pass={self.current_pass}")
                return

            self.current_pass += 1
            d.find_element(By.TAG_NAME, "body").send_keys(Keys.PAGE_DOWN)
            random_delay(2, 4)
            self.check_locked()
            if self.locked:
                logger.info(f"[partial_scrape_and_dm] => locked after PAGE_DOWN => pass={self.current_pass}")
                return

            pinned_messages_interaction(d)
            random_channel_switch(d)
            ensure_member_list_open(d)

            nameEls = d.find_elements(By.CSS_SELECTOR,
                "span.name_a31c43.username_de3235.desaturateUserColors_c7819f"
            )
            new_ = 0
            for e in nameEls:
                txt = None
                try:
                    rawt = e.text
                    if rawt:
                        txt = rawt.strip()
                except:
                    pass
                if txt and txt not in self.seen:
                    self.seen.add(txt)
                    user_count += 1
                    self.click_and_dm(e)
                    if self.locked:
                        logger.info(f"[partial_scrape_and_dm] => locked => pass={self.current_pass}")
                        return
                    new_ += 1
                    if max_users and user_count >= max_users:
                        logger.info("[partial_scrape_and_dm] => limit => done.")
                        return

            if new_ == 0:
                self.consecutive_fail_passes += 1
                logger.info(f"[partial_scrape_and_dm] => pass {self.current_pass}/{passes} => 0 new => maybe done.")
                if self.consecutive_fail_passes >= 10:
                    logger.info("[partial_scrape_and_dm] => 10 consecutive 0-new => stop => next token.")
                    return
            else:
                logger.info(f"[partial_scrape_and_dm] => new={new_}, total={user_count}")
                self.consecutive_fail_passes = 0

        logger.info(f"[partial_scrape_and_dm] => total => {user_count} after {passes} passes.")

    def re_land_channel(self):
        d = self.driver
        d.get(self.channel_url)
        time.sleep(3)
        ensure_member_list_open(d)
        for _ in range(self.current_pass):
            d.find_element(By.TAG_NAME, "body").send_keys(Keys.PAGE_DOWN)
            time.sleep(0.5)

    def click_and_dm(self, element):
        """
        Attempts to DM a user in the server. If there's a puzzle => uses 
        'solve_text_challenge_dm' => up to 3 sets x 3 puzzle steps = 9 attempts max.
        All button clicks use 'human_like_click' (ActionChains).
        """
        d = self.driver
        try:
            element.location_once_scrolled_into_view
            time.sleep(0.2)
            human_like_click(d, element)  # action-chains
            time.sleep(1)
        except Exception as e:
            logger.info(f"[click_and_dm] => cannot click user => {e}")
            return

        self.check_locked()
        if self.locked:
            return

        user_tag = None
        try:
            pop = WebDriverWait(d, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "span.userTagUsername_c32acf"))
            )
            user_tag = pop.text.strip()
        except:
            logger.info("[click_and_dm] => no popout => skip user.")
            return

        logger.info("-----------------------------------------------------")
        logger.info(f"[click_and_dm] => Attempting to DM user => {user_tag}")
        logger.info("-----------------------------------------------------")

        msg_btn = None
        try:
            msg_btn = WebDriverWait(d, 3).until(
                EC.element_to_be_clickable((By.XPATH,
                    "//div[@role='button' and (text()='Message' or contains(.,'Message @'))]"
                ))
            )
        except TimeoutException:
            pass

        if msg_btn:
            try:
                human_like_click(d, msg_btn)  # action-chains
                time.sleep(2)
                logger.info("[click_and_dm] => 'Message' button => clicked (ActionChains).")
            except Exception as e:
                logger.info(f"[click_and_dm] => can't click 'Message' => {e}")
        else:
            logger.info("[click_and_dm] => fallback => direct text area approach.")

        # puzzle check BEFORE DM => up to 3 sets x 3 steps => 9 attempts
        for attempt in range(2):
            self.check_locked()
            if self.locked:
                return

            open_text_challenge_flow(d)
            solve_text_challenge_dm(d)  # 3 sets approach for DM
            time.sleep(2)
            self.check_locked()
            if self.locked:
                return

            frames_again = d.find_elements(By.TAG_NAME, "iframe")
            puzzle_frames = [
                f for f in frames_again
                if "hcaptcha.com" in (f.get_attribute("src") or "")
            ]
            if puzzle_frames:
                logger.info("[click_and_dm] => puzzle re-appeared => do another attempt (DM approach).")
                time.sleep(2)
                continue
            else:
                logger.info("[click_and_dm] => no puzzle => proceed to send DM.")
                break

        # Attempt DM message
        try:
            paste_message_in_textarea(d, self.user_msg)
            self.check_locked()
            if self.locked:
                return

            if verify_dm_sent(d, self.user_msg, 5):
                logger.info("=====================================================")
                logger.info(f"[DM SENT SUCCESS] => user={user_tag}")
                logger.info(f"[DM CONTENT] => {self.user_msg}")
                logger.info("=====================================================")
            else:
                logger.warning("=====================================================")
                logger.warning(f"[DM NOT VERIFIED] => user={user_tag}")
                logger.warning(f"[DM ATTEMPTED CONTENT] => {self.user_msg}")
                logger.warning("=====================================================")
        except Exception as e:
            logger.info(f"[click_and_dm] => error typing DM => {e}")

        # puzzle check AFTER DM => again up to 3 sets x 3 steps
        for attempt in range(2):
            self.check_locked()
            if self.locked:
                return

            open_text_challenge_flow(d)
            solve_text_challenge_dm(d)  # 3 sets approach
            time.sleep(2)
            self.check_locked()
            if self.locked:
                return

            frames_again = d.find_elements(By.TAG_NAME, "iframe")
            puzzle_frames = [
                f for f in frames_again
                if "hcaptcha.com" in (f.get_attribute("src") or "")
            ]
            if puzzle_frames:
                logger.info("[click_and_dm] => puzzle re-appeared after DM => solve again (DM approach).")
                time.sleep(2)
                continue
            else:
                logger.info("[click_and_dm] => no puzzle after DM => done.")
                break

        # close user popout, wait, re-land the channel
        self.driver.switch_to.default_content()
        body_el = d.find_element(By.TAG_NAME, "body")
        body_el.send_keys(Keys.ESCAPE)
        time.sleep(1)
        dm_delay()
        self.re_land_channel()
        self.check_locked()


############################################################
# MAIN
############################################################

def main():
    logger.info("=== MAIN START (2x checkbox clicks) ===")

    if not os.path.exists(TOKENS_POOL_PATH):
        logger.warning(f"[MAIN] => No token file found at {TOKENS_POOL_PATH} => exit.")
        return

    with open(TOKENS_POOL_PATH, "r", encoding="utf-8") as tf:
        lines = [ln.strip() for ln in tf if ln.strip()]

    token_pool = lines[:MAX_POOL_SIZE]
    logger.info(f"[MAIN] => Loaded {len(token_pool)} tokens (max={MAX_POOL_SIZE}).")

    user_message = input("Enter DM message => ").strip()
    invite_link = input("Enter invite link => ").strip()
    headless_choice = input("Do you want to run in headless mode? [y/N]: ").strip().lower()
    headless_flag = headless_choice.startswith('y')

    passes_input = input("How many PAGE_DOWN passes? e.g. 10 => ").strip()
    try:
        passes_count = int(passes_input)
    except:
        passes_count = 10

    global_pass_progress = 0
    i = 0

    while i < len(token_pool):
        current_token = token_pool[i]
        logger.info(f"[MAIN] => Using token #{i+1}/{len(token_pool)} => {current_token[:10]}...")

        ds = DiscordServerDM(current_token)
        ds.user_msg = user_message
        ds.current_pass = global_pass_progress
        ds.setup_driver(headless=headless_flag)

        if not ds.token_login():
            logger.warning(f"[MAIN] => token {current_token[:10]} => cannot login => removing.")
            ds.driver.quit()
            token_pool.pop(i)
            continue

        # If there's an invite => normal join approach
        if invite_link:
            g_, c_ = ds.fetch_guild_and_channel(invite_link)
            if g_ and c_:
                ds.server_id = g_
                ds.channel_id = c_
                joined = ds.join_server(invite_link)
                logger.info(f"[MAIN] => join_server => joined={joined}, locked={ds.locked}")
                if ds.locked:
                    logger.warning(f"[MAIN] => token locked while joining => removing.")
                    ds.driver.quit()
                    token_pool.pop(i)
                    continue
                if not joined:
                    logger.info("[MAIN] => didn't join => skip DM for this token.")
            else:
                logger.info("[MAIN] => cannot fetch => skip DM for this token.")

        # If joined or server is set => do partial DM
        if ds.server_id and ds.channel_id and not ds.locked:
            ds.partial_scrape_and_dm(passes=passes_count)
            logger.info(f"[MAIN] => partial_scrape_and_dm => locked={ds.locked}, pass={ds.current_pass}")
            global_pass_progress = ds.current_pass

        locked_state = ds.locked
        ds.driver.quit()

        if locked_state:
            logger.warning(f"[MAIN] => token locked => remove => {current_token[:10]}...")
            token_pool.pop(i)
        else:
            i += 1

        logger.info(f"[MAIN] => global_pass_progress => {global_pass_progress}, pool_size => {len(token_pool)}")

        if global_pass_progress >= 999999:
            logger.info("[MAIN] => done => pass limit => break.")
            break

        if i >= len(token_pool):
            logger.info("[MAIN] => no more tokens => done.")
            break

    logger.info("[MAIN] => all done => finishing.")


if __name__ == "__main__":
    main()
