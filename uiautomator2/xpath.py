# coding: utf-8
#

from __future__ import absolute_import

import abc
import io
import json
import re
import logging
import threading
import time
import inspect
import functools
from collections import defaultdict
from types import ModuleType
from typing import Union

from logzero import setup_logger
from logzero import logger
from PIL import Image

from deprecated import deprecated
import adbutils
import uiautomator2
from uiautomator2.exceptions import XPathElementNotFoundError
from uiautomator2.utils import U
from uiautomator2.abcd import BasicUIMeta

try:
    from lxml import etree
except ImportError:
    logger.warning("lxml was not installed, xpath will not supported")


def safe_xmlstr(s):
    return s.replace("$", "-")


def init():
    uiautomator2.plugin_register("xpath", XPath)


def string_quote(s):
    """ quick way to quote string """
    return "{!r}".format(s)


def str2bytes(v) -> bytes:
    if isinstance(v, bytes):
        return v
    return v.encode('utf-8')


def strict_xpath(xpath: str, logger=logger) -> str:
    """ make xpath to be computer recognized xpath """
    orig_xpath = xpath

    if xpath.startswith('//'):
        pass
    elif xpath.startswith('@'):
        xpath = '//*[@resource-id={!r}]'.format(xpath[1:])
    elif xpath.startswith('^'):
        xpath = '//*[re:match(text(), {!r})]'.format(xpath)
    # elif xpath.startswith("$"):  # special for objects
    #     key = xpath[1:]
    #     return self(self.__alias_get(key), source)
    elif xpath.startswith('%') and xpath.endswith("%"):
        xpath = '//*[contains(@text, {})]'.format(string_quote(
            xpath[1:-1]))
    elif xpath.startswith('%'): # ends-with
        text = xpath[1:]
        xpath = '//*[{!r} = substring(@text, string-length(@text) - {} + 1)]'.format(text, len(text))
    elif xpath.endswith('%'): # starts-with
        text = xpath[:-1]
        xpath = "//*[starts-with(@text, {!r})]".format(text)
        # text = xpath[:-1]
        # for c in ".*[]?-":
        #     text = text.replace(c, "\\"+c)
        # xpath = '//*[ re:match(@text, {})]'.format(string_quote(text))
    else:
        xpath = '//*[@text={0} or @content-desc={0}]'.format(
            string_quote(xpath))

    logger.debug("xpath %s -> %s", orig_xpath, xpath)
    return xpath


class TimeoutException(Exception):
    pass


class XPathError(Exception):
    """ basic error for xpath plugin """


class UIMeta(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def click(self, x: int, y: int):
        pass
    
    @abc.abstractmethod
    def swipe(self, fx: int, fy: int, tx: int, ty: int, duration: float):
        """ duration is float type, indicate seconds """
    
    @abc.abstractmethod
    def window_size(self) -> tuple:
        """ return (width, height) """
    
    @abc.abstractmethod
    def dump_hierarchy(self) -> str:
        """ return xml content """
    
    @abc.abstractmethod
    def screenshot(self):
        """ return PIL.Image.Image """


class XPath(object):
    def __init__(self, d: "uiautomator2.Device"):
        """
        Args:
            d (uiautomator2 instance)
        """
        self._d = d
        assert hasattr(d, "click")
        assert hasattr(d, "swipe")
        assert hasattr(d, "window_size")
        assert hasattr(d, "dump_hierarchy")
        assert hasattr(d, "screenshot")
        assert hasattr(d, 'wait_timeout')

        self._click_before_delay = 0.0 # pre delay
        self._click_after_delay = None # post delay
        self._last_source = None
        self._event_callbacks = defaultdict(list)

        # used for click("#back") and back is the key
        self._alias = {}
        self._alias_strict = False
        self._dump_lock = threading.Lock()

        # 这里setup_logger不可以通过level参数传入logging.INFO
        # 不然其StreamHandler都要重新setLevel，没看懂也没关系，反正就是不要这么干. 特此备注
        self._logger = setup_logger()
        self._logger.setLevel(logging.INFO)

    def global_set(self, key, value):  #dicts):
        valid_keys = {
            "timeout", "alias", "alias_strict", "click_after_delay",
            "click_before_delay"
        }
        if key not in valid_keys:
            raise ValueError("invalid key", key)
        if key == "timeout":
            self.implicitly_wait(value)
        else:
            setattr(self, "_" + key, value)

    def implicitly_wait(self, timeout):
        """ set default timeout when click """
        self._d.wait_timeout = timeout
    
    @property
    def logger(self):
        expect_level = logging.DEBUG if self._d.settings['xpath_debug'] else logging.INFO
        if expect_level != self._logger.level:
            self._logger.setLevel(expect_level)
        return self._logger

    @property
    def wait_timeout(self):
        return self._d.wait_timeout
    
    @property
    def _watcher(self):
        return self._d.watcher

    def dump_hierarchy(self):
        with self._dump_lock:
            self._last_source = self._d.dump_hierarchy()
            return self._last_source
    
    def get_last_hierarchy(self):
        return self._last_source

    def add_event_listener(self, event_name, callback):
        self._event_callbacks[event_name] += [callback]

    def send_click(self, x, y):
        if self._click_before_delay:
            self.logger.debug("click before delay %.1f seconds",
                         self._click_after_delay)
            time.sleep(self._click_before_delay)

        # TODO(ssx): should use a better way
        # event callbacks for report generate
        for callback_func in self._event_callbacks['send_click']:
            callback_func(x, y)

        self._d.click(x, y)

        if self._click_after_delay:
            self.logger.debug("click after delay %.1f seconds",
                         self._click_after_delay)
            time.sleep(self._click_after_delay)

    def send_swipe(self, sx, sy, tx, ty):
        self._d.swipe(sx, sy, tx, ty)

    def send_text(self, text: str = None):
        self._d.set_fastinput_ime()
        self._d.clear_text()
        if text:
            self._d.send_keys(text)

    def take_screenshot(self) -> Image.Image:
        return self._d.screenshot()

    def match(self, xpath, source=None):
        return len(self(xpath, source).all()) > 0

    @deprecated(version="3.0.0", reason="use d.watcher.when(..) instead")
    def when(self, xquery: str):
        return self._watcher.when(xquery)

    @deprecated(version="3.0.0", reason="deprecated")
    def apply_watch_from_yaml(self, data):
        """
        Examples of argument data

            ---
            - when: "@com.example.app/popup"
            then: >
                def callback(d):
                    d.click(0.5, 0.5)
            - when: 继续
            then: click
        """
        try:
            import yaml
        except ImportError:
            self.logger.warning("missing lib pyyaml")

        data = yaml.load(data, Loader=yaml.SafeLoader)
        for item in data:
            when, then = item['when'], item['then']

            trigger = lambda: None
            self.logger.info("%s, %s", when, then)
            if then == 'click':
                trigger = lambda selector: selector.get_last_match().click()
                trigger.__doc__ = "click"
            elif then.lstrip().startswith("def callback"):
                mod = ModuleType("_inner_module")
                exec(then, mod.__dict__)
                trigger = mod.callback
                trigger.__doc__ = then
            else:
                self.logger.warning("Unknown then: %r", then)

            self.logger.debug("When: %r, Trigger: %r", when, trigger.__doc__)
            self.when(when).call(trigger)

    @deprecated(version="3.0.0", reason="use d.watcher.run() instead")
    def run_watchers(self, source=None):
        self._watcher.run()

    @deprecated(version="3.0.0", reason="use d.watcher.start(..) instead")
    def watch_background(self, interval: float = 4.0):
        return self._watcher.start(interval)

    @deprecated(version="3.0.0", reason="use d.watcher.stop() instead")
    def watch_stop(self):
        """ stop watch background """
        self._watcher.stop()

    @deprecated(version="3.0.0", reason="use d.watcher.remove() instead")
    def watch_clear(self):
        self._watcher.stop()

    @deprecated(version="3.0.0", reason="removed")
    def sleep_watch(self, seconds):
        """ run watchers when sleep """
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.run_watchers()
            left_time = max(0, deadline - time.time())
            time.sleep(min(0.5, left_time))

    def click(self, xpath, source=None, watch=None, timeout=None, pre_delay:float=None):
        """
        Args:
            xpath (str): xpath string
            watch (bool): click popup elements (deprecated)
            timeout (float): pass
            pre_delay (float): pre delay wait time before click

        Raises:
            TimeoutException
        """
        timeout = timeout or self.wait_timeout
        self.logger.info("XPath(timeout %.1f) %s", timeout, xpath)

        # d.set_run_watcher_before_click(True)
        # watch = False
        # if watch is None:
        #     watch = not self._watcher.running()

        deadline = time.time() + timeout
        while True:
            source = self.dump_hierarchy()
            # if watch and self._watcher.run(source):
            #     time.sleep(.5)  # post delay
            #     deadline = time.time() + timeout # correct deadline
            #     continue

            selector = self(xpath, source)
            if selector.exists:
                if pre_delay:
                    self.logger.debug("pre-delay %.1f seconds", pre_delay)
                    time.sleep(pre_delay)
                selector.get_last_match().click()
                time.sleep(.5)  # post sleep
                return

            if time.time() > deadline:
                break
            time.sleep(.5)

        raise TimeoutException("timeout %.1f, xpath: %s" % (timeout, xpath))

    def __alias_get(self, key, default=None):
        """
        when alias_strict set, if key not in _alias, XPathError will be raised
        """
        value = self._alias.get(key, default)
        if value is None:
            if self._alias_strict:
                raise XPathError("alias have not found key", key)
            value = key
        return value

    def __call__(self, xpath: str, source=None):
        # print("XPATH:", xpath)
        return XPathSelector(self, xpath, source)


class XPathSelector(object):
    def __init__(self, parent: XPath, xpath: Union[list, str], source=None):
        self.logger = parent.logger

        self._parent = parent
        self._d = parent._d
        self._xpath_list = [strict_xpath(xpath, self.logger)] if isinstance(xpath, str) else xpath
        self._source = source
        self._last_source = None
        self._watchers = []
        
        self.click = functools.partial(self._parent.click, self._xpath_list) # parent click

    def xpath(self, xpath: str):
        xpath = strict_xpath(xpath, self.logger)
        self._xpath_list.append(xpath)
        return self

    @property
    def _global_timeout(self):
        return self._parent.wait_timeout

    def all(self, source=None):
        """
        Returns:
            list of XMLElement
        """
        xml_content = source or self._source or self._parent.dump_hierarchy()
        self._last_source = xml_content

        root = etree.fromstring(str2bytes(xml_content))
        for node in root.xpath("//node"):
            node.tag = safe_xmlstr(node.attrib.pop("class", "")) or "node"
        
        match_sets = []
        for xpath in self._xpath_list:
            matches = root.xpath(xpath,
                namespaces={"re": "http://exslt.org/regular-expressions"})
            match_sets.append(matches)
        # find out nodes which match all xpaths
        match_nodes = functools.reduce(lambda x, y: set(x).intersection(y), match_sets)
        return [XMLElement(node, self._parent) for node in match_nodes]

    @property
    def exists(self):
        return len(self.all()) > 0

    def get(self):
        """
        Get first matched element

        Returns:
            XMLElement
        
        Raises:
            XPathElementNotFoundError
        """
        if not self.wait(self._global_timeout):
            raise XPathElementNotFoundError(self._xpath_list)
        return self.get_last_match()

    def get_last_match(self):
        return self.all(self._last_source)[0]

    def get_text(self):
        """
        get element text
        
        Returns:
            string of node text

        Raises:
            XPathElementNotFoundError
        """
        return self.get().attrib.get("text", "")

    def set_text(self, text: str = ""):
        el = self.get()
        self._parent.send_text()  # switch ime
        el.click()  # focus input-area
        self._parent.send_text(text)

    def wait(self, timeout=None):
        """
        Args:
            timeout (float): seconds

        Raises:
            None or XMLElement
        """
        deadline = time.time() + (timeout or self._global_timeout)
        while time.time() < deadline:
            if self.exists:
                return self.get_last_match()
            time.sleep(.2)
        return None
    
    def wait_gone(self, timeout=None):
        """
        Args:
            timeout (float): seconds
        
        Returns:
            True if gone else False
        """
        deadline = time.time() + (timeout or self._global_timeout)
        while time.time() < deadline:
            if not self.exists:
                return True
            time.sleep(.2)
        return False

    def click_nowait(self):
        x, y = self.all()[0].center()
        self.logger.info("click %d, %d", x, y)
        self._parent.send_click(x, y)

    def screenshot(self) -> Image.Image:
        el = self.get()
        return el.screenshot()


class XMLElement(object):
    def __init__(self, elem, parent: XPath):
        """
        Args:
            elem: lxml node
            d: uiautomator2 instance
        """
        self.elem = elem
        self._parent = parent

    def center(self):
        """
        Returns:
            (x, y)
        """
        return self.offset(0.5, 0.5)

    def offset(self, px: float = 0.0, py: float = 0.0):
        """
        Offset from left_top

        Args:
            px (float): percent of width
            py (float): percent of height
        
        Example:
            offset(0.5, 0.5) means center
        """
        x, y, width, height = self.rect
        return x + int(width * px), y + int(height * py)

    def click(self):
        """
        click element
        """
        x, y = self.center()
        self._parent.send_click(x, y)

    def screenshot(self):
        """
        Take screenshot of element
        """
        im = self._parent.take_screenshot()
        return im.crop(self.bounds)

    def swipe(self, direction: str, scale: float = 0.9):
        """
        Args:
            direction: one of ["left", "right", "up", "down"]
            scale: percent of swipe, range (0, 1.0)
        """
        def _swipe(_from, _to):
            self._parent.send_swipe(_from[0], _from[1], _to[0], _to[1])

        assert 0 < scale <= 1.0

        lx, ly, rx, ry = self.bounds
        width, height = rx - lx, ry - ly

        h_offset = int(width * (1 - scale)) // 2
        v_offset = int(height * (1 - scale)) // 2

        left = lx+h_offset, ly + height // 2
        up = lx + width // 2, ly + v_offset
        right = rx - h_offset, ly + height // 2
        bottom = lx + width // 2, ry - v_offset

        if direction == "left":
            _swipe(right, left)
        elif direction == "right":
            _swipe(left, right)
        elif direction == "up":
            _swipe(bottom, up)
        elif direction == "down":
            _swipe(up, bottom)
        else:
            raise RuntimeError("Unknown direction:", direction)

    def percent_size(self):
        """ Returns:
                (float, float): eg, (0.5, 0.5) means 50%, 50%
        """
        ww, wh = self._parent.window_size()
        _, _, w, h = self.rect
        return (w/ww, h/wh)
        
    @property
    def bounds(self):
        """
        Returns:
            tuple of (left, top, right, bottom)
        """
        bounds = self.elem.attrib.get("bounds")
        lx, ly, rx, ry = map(int, re.findall(r"\d+", bounds))
        return (lx, ly, rx, ry)

    @property
    def rect(self):
        """
        Returns:
            (left_top_x, left_top_y, width, height)
        """
        lx, ly, rx, ry = self.bounds
        return lx, ly, rx - lx, ry - ly

    @property
    def text(self):
        return self.elem.attrib.get("text")

    @property
    def attrib(self):
        return self.elem.attrib
    
    @property
    def info(self):
        ret = {}
        for key in ("text", "focusable", "enabled", "focused", "scrollable", "selected"):
            ret[key] = self.attrib.get(key)
        ret["className"] = self.elem.tag
        lx, ly, rx, ry = self.bounds
        ret["bounds"] = {'left': lx, 'top': ly, 'right': rx, 'bottom': ry}
        ret["contentDescription"] = self.attrib.get("content-desc")
        ret["longClickable"] = self.attrib.get("long-clickable")
        ret["packageName"] = self.attrib.get("package")
        return ret


class AdbUI(BasicUIMeta):
    """
    Use adb command to run ui test
    """
    def __init__(self, d: adbutils.AdbDevice):
        self._d = d

    def click(self, x, y):
        self._d.click(x, y)
    
    def swipe(self, sx, sy, ex, ey, duration):
        self._d.swipe(sx, sy, ex, ey, duration)
    
    def window_size(self):
        w, h = self._d.window_size()
        return w, h
    
    def dump_hierarchy(self):
        return self._d.dump_hierarchy()
    
    def screenshot(self):
        d = self._d
        json_output = d.shell(["LD_LIBRARY_PATH=/data/local/tmp", "/data/local/tmp/minicap", "-i", "2&>/dev/null"]).strip()
        data = json.loads(json_output)
        w, h, r = data["width"], data["height"], data["rotation"]
        remote_image_path = "/sdcard/minicap.jpg"
        d.shell(["rm", remote_image_path])
        d.shell([
            "LD_LIBRARY_PATH=/data/local/tmp", 
            "/data/local/tmp/minicap", 
            "-P", "{0}x{1}@{0}x{1}/{2}".format(w, h, r), 
            "-s", ">"+remote_image_path])

        if d.sync.stat(remote_image_path).size == 0:
            raise RuntimeError("screenshot using minicap error")
        
        buf = io.BytesIO()
        for data in d.sync.iter_content(remote_image_path):
            buf.write(data)
        return Image.open(buf)



if __name__ == "__main__":
    d = AdbUI(adbutils.adb.device())
    xpath = XPath(d)
    # print(d.screenshot())
    # print(d.dump_hierarchy()[:20])
    xpath("App").click()
    xpath("Alarm").click()
    # init()
    # import uiautomator2.ext.htmlreport as htmlreport

    # d = uiautomator2.connect()
    # hrp = htmlreport.HTMLReport(d)

    # # take screenshot before each click
    # hrp.patch_click()
    # d.app_start("com.netease.cloudmusic", stop=True)

    # # watchers
    # d.ext_xpath.when("跳过").click()
    # # d.ext_xpath.when("知道了").click()

    # # steps
    # d.ext_xpath("//*[@text='私人FM']/../android.widget.ImageView").click()
    # d.ext_xpath("下一首").click()
    # d.ext_xpath.sleep_watch(2)
    # d.ext_xpath("转到上一层级").click()
