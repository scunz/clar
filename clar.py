#!/usr/bin/env python

from __future__ import with_statement
from string import Template
import re, fnmatch, os, codecs

VERSION = "0.10.0"

TEST_FUNC_REGEX = r"^(void\s+(test_%s__(\w+))\(\s*void\s*\))\s*\{"

EVENT_CB_REGEX = re.compile(
    r"^(void\s+clar_on_(\w+)\(\s*void\s*\))\s*\{",
    re.MULTILINE)

SKIP_COMMENTS_REGEX = re.compile(
    r'//.*?$|/\*.*?\*/|\'(?:\\.|[^\\\'])*\'|"(?:\\.|[^\\"])*"',
    re.DOTALL | re.MULTILINE)

CATEGORY_REGEX = re.compile(r"CL_IN_CATEGORY\(\s*\"([^\"]+)\"\s*\)")

CLAR_HEADER = """
/*
 * Clar v%s
 *
 * This is an autogenerated file. Do not modify.
 * To add new unit tests or suites, regenerate the whole
 * file with `./clar`
 */
""" % VERSION

CLAR_EVENTS = [
    'init',
    'shutdown',
    'test',
    'suite'
]

def main():
    from optparse import OptionParser

    parser = OptionParser()

    parser.add_option('-c', '--clar-path', dest='clar_path')
    parser.add_option('-v', '--report-to', dest='print_mode', default='default')

    options, args = parser.parse_args()

    for folder in args or ['.']:
        builder = ClarTestBuilder(folder,
            clar_path = options.clar_path,
            print_mode = options.print_mode)

        builder.render()


class ClarTestBuilder:
    def __init__(self, path, clar_path = None, print_mode = 'default'):
        self.declarations = []
        self.suite_names = []
        self.callback_data = {}
        self.suite_data = {}
        self.category_data = {}
        self.event_callbacks = []

        self.clar_path = os.path.abspath(clar_path) if clar_path else None

        self.path = os.path.abspath(path)
        self.modules = [
            "clar_sandbox.c",
            "clar_fixtures.c",
            "clar_fs.c",
            "clar_categorize.c",
        ]

        self.modules.append("clar_print_%s.c" % print_mode)

        print("Loading test suites...")

        for root, dirs, files in os.walk(self.path):
            module_root = root[len(self.path):]
            module_root = [c for c in module_root.split(os.sep) if c]

            tests_in_module = fnmatch.filter(files, "*.c")

            for test_file in tests_in_module:
                full_path = os.path.join(root, test_file)
                test_name = "_".join(module_root + [test_file[:-2]])

                with codecs.open(full_path, 'r', 'utf-8') as f:
                    self._process_test_file(test_name, f.read())

        if not self.suite_data:
            raise RuntimeError(
                'No tests found under "%s"' % path)

    def render(self):
        main_file = os.path.join(self.path, 'clar_main.c')
        with open(main_file, "w") as out:
            out.write(self._render_main())

        header_file = os.path.join(self.path, 'clar.h')
        with open(header_file, "w") as out:
            out.write(self._render_header())

        print ('Written Clar suite to "%s"' % self.path)

    #####################################################
    # Internal methods
    #####################################################

    def _render_cb(self, cb):
        return '{"%s", &%s}' % (cb['short_name'], cb['symbol'])

    def _render_suite(self, suite, index):
        template = Template(
r"""
    {
        ${suite_index},
        "${clean_name}",
        ${initialize},
        ${cleanup},
        ${categories},
        ${cb_ptr}, ${cb_count}
    }
""")

        callbacks = {}
        for cb in ['initialize', 'cleanup']:
            callbacks[cb] = (self._render_cb(suite[cb])
                if suite[cb] else "{NULL, NULL}")

        if len(self.category_data[suite['name']]) > 0:
            cats = "_clar_cat_%s" % suite['name']
        else:
            cats = "NULL"

        return template.substitute(
            suite_index = index,
            clean_name = suite['name'].replace("_", "::"),
            initialize = callbacks['initialize'],
            cleanup = callbacks['cleanup'],
            categories = cats,
            cb_ptr = "_clar_cb_%s" % suite['name'],
            cb_count = suite['cb_count']
        ).strip()

    def _render_callbacks(self, suite_name, callbacks):
        template = Template(
r"""
static const struct clar_func _clar_cb_${suite_name}[] = {
    ${callbacks}
};
""")
        callbacks = [
            self._render_cb(cb)
            for cb in callbacks
            if cb['short_name'] not in ('initialize', 'cleanup')
        ]

        return template.substitute(
            suite_name = suite_name,
            callbacks = ",\n\t".join(callbacks)
        ).strip()

    def _render_categories(self, suite_name, categories):
        template = Template(
r"""
static const char *_clar_cat_${suite_name}[] = { "${categories}", NULL };
""")
        if len(categories) > 0:
            return template.substitute(
                suite_name = suite_name,
                categories = '","'.join(categories)
                ).strip()
        else:
            return ""

    def _render_event_overrides(self):
        overrides = []
        for event in CLAR_EVENTS:
            if event in self.event_callbacks:
                continue

            overrides.append(
                "#define clar_on_%s() /* nop */" % event
            )

        return '\n'.join(overrides)

    def _render_header(self):
        template = Template(self._load_file('clar.h'))

        declarations = "\n".join(
            "extern %s;" % decl
            for decl in sorted(self.declarations)
        )

        return template.substitute(
            extern_declarations = declarations,
        )

    def _render_main(self):
        template = Template(self._load_file('clar.c'))
        suite_names = sorted(self.suite_names)

        suite_data = [
            self._render_suite(self.suite_data[s], i)
            for i, s in enumerate(suite_names)
        ]

        callbacks = [
            self._render_callbacks(s, self.callback_data[s])
            for s in suite_names
        ]

        callback_count = sum(
            len(cbs) for cbs in self.callback_data.values()
        )

        categories = [
            self._render_categories(s, self.category_data[s])
            for s in suite_names
        ]

        return template.substitute(
            clar_modules = self._get_modules(),
            clar_callbacks = "\n".join(callbacks),
            clar_categories = "".join(categories),
            clar_suites = ",\n\t".join(suite_data),
            clar_suite_count = len(suite_data),
            clar_callback_count = callback_count,
            clar_event_overrides = self._render_event_overrides(),
        )

    def _load_file(self, filename):
        if self.clar_path:
            filename = os.path.join(self.clar_path, filename)
            with open(filename) as cfile:
                return cfile.read()

        else:
            import zlib, base64, sys
            content = CLAR_FILES[filename]

            if sys.version_info >= (3, 0):
                content = bytearray(content, 'utf_8')
                content = base64.b64decode(content)
                content = zlib.decompress(content)
                return str(content, 'utf-8')
            else:
                content = base64.b64decode(content)
                return zlib.decompress(content)

    def _get_modules(self):
        return "\n".join(self._load_file(f) for f in self.modules)

    def _skip_comments(self, text):
        def _replacer(match):
            s = match.group(0)
            return "" if s.startswith('/') else s

        return re.sub(SKIP_COMMENTS_REGEX, _replacer, text)

    def _process_test_file(self, suite_name, contents):
        contents = self._skip_comments(contents)

        self._process_events(contents)
        self._process_declarations(suite_name, contents)
        self._process_categories(suite_name, contents)

    def _process_events(self, contents):
        for (decl, event) in EVENT_CB_REGEX.findall(contents):
            if event not in CLAR_EVENTS:
                continue

            self.declarations.append(decl)
            self.event_callbacks.append(event)

    def _process_declarations(self, suite_name, contents):
        callbacks = []
        initialize = cleanup = None

        regex_string = TEST_FUNC_REGEX % suite_name
        regex = re.compile(regex_string, re.MULTILINE)

        for (declaration, symbol, short_name) in regex.findall(contents):
            data = {
                "short_name" : short_name,
                "declaration" : declaration,
                "symbol" : symbol
            }

            if short_name == 'initialize':
                initialize = data
            elif short_name == 'cleanup':
                cleanup = data
            else:
                callbacks.append(data)

        if not callbacks:
            return

        tests_in_suite = len(callbacks)

        suite = {
            "name" : suite_name,
            "initialize" : initialize,
            "cleanup" : cleanup,
            "cb_count" : tests_in_suite
        }

        if initialize:
            self.declarations.append(initialize['declaration'])

        if cleanup:
            self.declarations.append(cleanup['declaration'])

        self.declarations += [
            callback['declaration']
            for callback in callbacks
        ]

        callbacks.sort(key=lambda x: x['short_name'])
        self.callback_data[suite_name] = callbacks
        self.suite_data[suite_name] = suite
        self.suite_names.append(suite_name)

        print("  %s (%d tests)" % (suite_name, tests_in_suite))

    def _process_categories(self, suite_name, contents):
        self.category_data[suite_name] = [
            cat for cat in CATEGORY_REGEX.findall(contents) ]


CLAR_FILES = {
"clar.c" : r"""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""",
"clar_print_default.c" : r"""eJyFU8Fu2zAMPdtfwQUwIgVuenew9tZTsMuwU1sYqiW3AhzJkOhswNB/n0Q5rRws6Ukmxff4RD6XHgXqDo5WS+gG4drRaYOtNhpZ+ABUHtvOTgZriLGfNKpTorPGI3RvwsEmXRhxUJ6Xf8uCRUr+Cd+VBVH3bLW3QioJlUxsvoHKP5lVDbEjX3TIWTOGnygcKhlAIftelhde4d8mlPa3+folMaGcsy4lLr0gpTLkRy4D78pPoU8maSxIlVOjddhSrWdXpVMN6TbT4TRpj27qMJVRAWzoILmnlhAGy+FB6GFyqqG5Bgqeq6p801QeWOU5PIagks/weIPhiOVlURDrzR09NIvjLGK4Mhak8p3TI2q7gPR6yBGDNmF90+FFuTOeObvQBScjzHVpqAf/SlW6BzZfZM3h23f48Wu/54H+Ek9Wzpfbue4fa6JSlts8SQ9+TJ7JXpISfZi7kuf+iYDdMkOYzNJVF/QmNNzD+mENDay36y/00YbY///D3ObaSPWHVN1uwFg7wuZ2aWeqOLN4kn2tv3gJhl70D9uqYbvdUrOjaAcdroR7HXcU+vjnshjXkBZbHPt5Bh5lWBjla4LwhFFGsjl8L/8BsUiTTQ==""",
"clar_print_tap.c" : r"""eJyNVE1vnDAQPcOvmGWFBAiQot6yaqr2HFU9tLdKyAGzscLayDbbVlX+e8cDJPbuJtsTzPObmTcfdmwss6KFoxIdtAPTzaiFtI2Qwmb4A5Yb27RqkrYEZ5tJWL4CrZLGQvvINBTzgWQHbvL4bxxlLmT+6r5bIY94gq08ktBnyffP3+DItRFKws2HnzLJd/FzHL8h2TxOtlO/5HXZDuBaKz0D/yM3xDznXRxHoodsEwSMXmrYwsiM4R2wYYC0I2GZybGY0hOJhUV8MDxw7JkY0BGd2EHJ/am3l7BEvyiMtoa5qeu0O8/2dhspLPVQTod1xMbqqbUzjQhQ0MdrHbJdL9a8AFVVzSPzMJy5YXsOt5Ca1yKqu7mWg9mHdMNx/ML+uaVenEWj0QCcRSM8pLri4QLV4SGzx6ZfYjo8ZA5CrszOZzq8wXY8cJ2v67Ecddy0WozWbfTmI3z9cX/vLwuARzgV4B3lYafrur52OZSk1fEvLO2Du4bzhZhNUj0D8/rRhNdUqXFLWC3CUPiyop8gkcqCekqwGQl+3Jkf8MXEdHFE8kmc5qPSy86Z7EoFNNbs8pvj33IhO/470L2FoihQNWTbtMudQY313X3X92WwB5QcyMC9Ld0QKOeRNYPAI6b3445MjIQOzi5hWfF+UWbwxZrwRUq+YCMBfzdAO348JVAKFyKfY3LZZYv5HP8D5Mbj9w==""",
"clar_sandbox.c" : r"""eJydVWtP4kAU/dz+iism0gpKfWQ3G9YPm+gasioEMJgomdR2KhPplMwM7KLxv++dTqEP0DVrTKjcO+eec+6cKpWvWADBxBdAgqkvyMxXk/tT79uXcdu2pSkzrmwmycKfspCoeJY2OUHCpTJH9/UXrv1qW4PhjyEZglR42mIROBrC0eUm7Enlws4ZeK5tWYKqueDgrfp2BqQzOO/08cChVCROQupW+7Jnxw8CKmWGOiLdXy6cadi2/VbiHDFe5JsyfZxHERVNkOyFEgVTyp8M9V0W8ZBGQEadm5Nj28pwjMqse4EGBcmcKziD03alx+BTvkCjhLwfYw8aYtWG1z3UVWuCfko/Lszn7eCi3+t3f3auLmo2WG8oEaxsEtN6o0SAwxDHawOD7/n4NjQazE3hK7Ox+YkqfHDWRNgYjbGMyfilNlWfUozPqZ6SVjbXq1vNCJQpeDBbOivvsNRcOaehC0uyrDcbf22rtQ+dCNSE6m4mEh5TtC1MqOR19NNfgs+XasL4UxOUWIJKYC4ptHA+7Lfsd0jVdL2W8arSMsUSswIxJLVLp5Ia6EuqhjSe9TSocz7q9s9dc6wJBq5y+XYpD1lkdA0nTIJcSkXjtaApe6YooKRFiw/mQqTCmaCBSrD4gbjDd5UdfiRr9efBUTEAi4SFkEZ6zqXPw8fkj6O/S2OqCRTy7o11gOoPXj1XjVcDI1FMRDBBFcgSaRYMiSQRcQGsmkL0k01DklEwStc8CrdXF4jy2TRNTi3F09bcpT81nbZ1ZFcvjXLAcw4m3klUpOVigIpvHu2WbSEYTkO/8aEsoqr+FXD1PBExLu2FpnT1onvdQecOMKm/fRGCnPpyQmW65EKUrY0oaxF5iKv7YNk+HtJ9WFalBPVWfR219SIqGFrZARyN9RsX+82gcr3RyMH0PVpdu7wLGpppM1/ONmdxDDZllgF6xjgNHUKuOzeXo5NjQtyMXPyMkZmVjqLMm9urq4296P74Wd+34la9r5638S9EH8BkF0enKytPJfKf92ML7v8QWb1i8NQn5a5XmOe6HKEU4fMhhr29banbngCNYpJdJLrVixK9v7GvgW8=""",
"clar_fixtures.c" : r"""eJyFUV1LwzAUfW5+xZU9rLUVJ4ggZQ9DFAUfZEwQSglZmrBAl5Qkk6n43236tWbKfMvNOfecc+81llhBgSppLNAN0XCOuNjbnWa4InYTjpE1MSzxuD1Vki2L0BcKTKfn0EYgu57d3uRpjYhPhi1opSwumUwRCvo3zMFYXT9C5xA5stWSVh9hI5FAa+wUFG//osgJCA5tmQ1SF3CVw9kcppfTCAWBj8ZxDg3UN4/zZ7MaHBrHSBw7vpcJ4mGS5Ijtai9qnannNqk1q7myXU+KvhGaCF4wDnfPiyV+eHpbvS7v8cti9YjGq6Yl7lzCkxfo1L0j/lJOwOtrUrwrUcDBBRsii7Xan3bjBlNVL2WUzuMkgGlJdLuIP21oyYjcVf/a6G3ozXTQPRqmsZkwWQiOfgAVGffP""",
"clar_fs.c" : r"""eJylVdtu20YQfSa/YkAD8TKWY8dJX6L0wXDEVqgsBhINN7UFhiGX1qIkl9hd+dLG/57ZCynJUWEkfZE0s7NnZufMGe2xsqAlpJfj6ZsT399DgzUUojhKo8npb3Mg+ud8PBlNE/hq/NP4LJ5G49n5aTKOp71zNJvFs4vx06DzPz6MZ6HvS5UplkO+zAS89EtWUd7KtM3UkuS8kcqdGE/o/+t71tYm/ArTi8lk6HuS/UNTBRVtbtRyAGzo+x4rgaQ2zMaFvucJqlaicdd8z15AHKkE/rbxIQI6+DqrKp4TF3YAJ2GH/AxwTeu8fTBRA0jtl0Xp0K+sucAsx9suzPPauX2v5AIIMxYweO9AhnBwwELAbvTFXLGFrmf/aF+X4/Uu2L++3scEjwjmitRnQ/+x7/0tZ0XXecIaBTUv6AC22i/5SuRPnQWVynAy/z3CSYg/zpPZxVkCJQLp4m2YvYqVbJHrEHU7bJgG+y7IZNBQf1HBz2nNxQN5oeEHoDnnJdlOHYa2aa18dRetmlxziI8ZOl8bCV5ruk3u3ptw9OlUnaeMquxGorOfd/OcKs2kpEKlBFuMibHUuKUCm8gbW1aoOTge4HFwyZqC30l4EgdlhmYR+J4tVVBK1q0wpnv0U4JkKmqygxTDQEdfFKcfRpNRMsKx6zgzM7oLL+c4oz9A80aSs/jjp40U6bpmA46t0vgVzZpVS7TLApg3lOwe55A6ivMqE04hwcsgtCB7tJK0KxdH0pdLWlUpXylii3IVZuLm9mphsPXg6gsrqeXECtwH+Kl7jF96sLj4m6z1i773cGw1VLYCb5dEqoIKodnzgvmDVLQGtLl4B5/t7c+Q40ZwFL66bgLNmUfvmSKHr0Onsg5eT4LFp/c0vyWm1uPFwBTdBd9lTGGwvjCAF7b+Ad4b9mq9HP05TubJaXIxJ/b8f3DZU2lNU9Ivi+G2VNcL1dopLh3dt17IuC0LpHVDwuvA9TLtT21LrHm1EXlo9ly/s/4rwC5C1z00g6MvrDnK22DovCYoOJz1jpPFpsaN6412udkJndTNwdtF/zdiFF6vpMJxlNKIfD12hjQj7MiwD4qD7jkovbfcSEvtlVlTfOH3uxX+rKg3NL3B0dvFrh6I+rselNtN6F68oxk/+2araVBLuv3SZ6RvZL5q3BVi9r52bTgeUfZNwUr/G9kaoSs=""",
"clar_categorize.c" : r"""eJydVV1P2zAUfU5+xaUTlfMBgueuSBVje6k2CcHDxFDkpu5mKSSd7U5jaP9919dJcUKSwl7a2L5f59xz7XdrsZGlgMvl4jq7XNxcffpy/TX7cPVxcbu8gQme8l1hJmFoHrcCV6CN2uUGnsIgr0ptIP/BFcRxyR+EnoWBLHGr2pWm/uZFUeWz8C/kBVdZzo34XqnHrJAaLUJtuJF5zxlk/p78IzJR8lUh1s9OlKnlKEvyZT3hYvubgl8yGkThk6tSYrWbSgGTMIezGUh4D9bj5MJhgSSRURgEcgMMCci5FvnDlmGItLYj/HfyPoI5RrC2gRJmp0o4x9j15xkSsa//VyXXHQB8vc5M9V8gsLB+MmoftLNFuUKQRPLwIMLFvEZCHYtsgwNvA5I5nGP9zSbhRbJYSwWREmTNLH5GCOPIc0j9HBCDxs5Wm1aMKMIkyJKf584rNEnuMS3iOcCl0wvrkEVnxNgw89Mh7RGNLsKrkmcIk1mImQG9k0ZkhKDpYD1J5EnHENOfpvpqP6vMFsWEvFt+DbZ3iCNr3hW3Vw5uJInVBtlgSLRydaCcTxsWcLgtephOiUdaOLf+UkiZPksvx5UmiKA5ofCGGLdcEQ315JyAN3Y8XR1qwFZhvqZvLRFsFV54v/3JD4OfulKGNXodav+pkzBCHrRw/UWLPmGnL/H39mY8+v4uIogbNjnWx/pbOUnBIUuH/fd31HhfpHZy7NA3JFc3Tb5Edc73V8zROBoL5I1SpqiUGW+EvTqfL7eBR8EqdWSyD4yOhdTMhZ92BoQWjubw+Xa5nIFdubkYvpJHyqBoUef5aL8f/wB2SZXK""",
"clar.h" : r"""eJy9VV1vmzAUfS6/wg17gAi1TR+7tlIUJWukqJvaVNueLGNMsQaG2mbNNO2/7xpICF/Juoe+JOZe33uOOecam4ciYCHCeLaaPuD1/HGN7zC2bAhywTpxy+aCxnnA0LXSQcz9s+jWsn6mPEA0JhJjohST2rFOuNCIpiLgmqfCs05grSASEYnGIY+ZV26JAaWVZVKmshULmKKSZ1UvU6iiNI8DTPxUavdjDwfMXnISY+Xs9/GGH6BpJwCNh/pyxxT0FfV12bbBimlMY0ZEnjlFzBlXj275PHY9VC7SjLzkrKaAQ9UoNW1tHhr5CpEWy2/rp4c5/jJd31n7HEwp3+hcMqepQhHDgiQNlCqsiAj8dPOWki27AyU2A0uE1s5gsxVe3uPZdD3/9PnhuwML17LOx2MLjdG0eN8gOUoIlalCr1xHiG2ymFOuUeETlDClyDOD/ee7pkApyZXGGSiGHSiQHjIOcpsmLTIuur1BFx44fbFczTE2q9XyvliNFrmgBQFK4hiFBHwbXKERsuueHpq4HWCz8zjw9SDufJMxqlmAwgYBnRYcjjCobHoU/nT43IAv4b0aYK6QSDXSMmd9uFutZhGjP/5DJ2rq3kmoC7eL/M5K9VF4B6Eujg2VSP9xnCpKfRN2/7Ra9Y9Cq2j/nXeKqqPvKppuLrcPm+7YOWq71QhdC3ZI1V5plx08S7GlXdF7kkUqqTERdIPL8vyVSMHFc5t9QaDHJ0PuWDO4hsthOBv1XxYV0lu6fi1LUJBL86cNCNswmhtXXY16PLf+lcHhSMt57dO1Pttq4qnLJqVdDpKu50Da2zHcERw96oJXwlVCNI2KYVAT+IU5MsvLgQtz912feLwfmDuQBGDeC2zzGoQfBvEdf+L5QyCnp5B2PfPXD+TXQP5hoMzJJl52uTdJDkRcdHODHAjvSWRUTJiO0gD0M7SIkaoU6cNvttFMCryf+WNtP+Z/AaQwXp0="""
}
if __name__ == '__main__':
    main()
