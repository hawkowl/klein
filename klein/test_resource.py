import os

from StringIO import StringIO

from twisted.trial import unittest

from klein import Klein

from klein.interfaces import IKleinRequest
from klein.resource import KleinResource, ensure_utf8_bytes

from twisted.internet.defer import succeed, Deferred, fail, CancelledError
from twisted.internet.error import ConnectionLost
from twisted.internet.task import Clock
from twisted.web import server
from twisted.web.static import File
from twisted.web.resource import Resource
from twisted.web.template import Element, XMLString, renderer
from twisted.web.test.test_web import DummyChannel
from twisted.web.http_headers import Headers

from werkzeug.exceptions import NotFound

from mock import Mock, call


def requestMock(path, method="GET", host="localhost", port=8080, isSecure=False,
                body=None, headers=None):
    if not headers:
        headers = {}

    if not body:
        body = ''

    request = server.Request(DummyChannel(), False)
    request.site = Mock(server.Site)
    request.gotLength(len(body))
    request.content = StringIO()
    request.content.write(body)
    request.content.seek(0)
    request.requestHeaders = Headers(headers)
    request.setHost(host, port, isSecure)
    request.uri = path
    request.prepath = []
    request.postpath = path.split('/')[1:]
    request.method = method
    request.clientproto = 'HTTP/1.1'

    request.setHeader = Mock(wraps=request.setHeader)
    request.setResponseCode = Mock(wraps=request.setResponseCode)

    request.testClock = Clock()
    request._written = StringIO()

    request._finishCalled = 0
    request._writeCalled = 0

    def produce():
        while request.producer:
            request.producer.resumeProducing()

    def registerProducer(producer, streaming):
        request.producer = producer
        if streaming:
            request.producer.resumeProducing()
        else:
            request.testClock.callLater(0.0, produce)
            request.testClock.advance(0)

    def unregisterProducer():
        request.producer = None

    def finish():
        request._finishCalled += 1

        if not request.startedWriting:
            request.write('')

        if not request.finished:
            request.finished = True
            request._cleanup()

    def write(data):
        request._writeCalled += 1
        request.startedWriting = True

        if not request.finished:
            request._written.write(data)
        else:
            raise RuntimeError('Request.write called on a request after '
                'Request.finish was called.')

    def assertWritten(data):
        responseData = request._written.getvalue()
        if not responseData == data:
            raise AssertionError("Same data was not written!\ngot: %s"
                "\nexpected: %s" % (responseData, data))
        else:
            return True

    def assertWrittenOnceWith(data):
        if request.assertWritten(data) and request._writeCalled == 1:
            return True
        else:
            raise AssertionError("request.write was called %s times, "
                "expected 1" % (request._writeCalled,))

    def assertFinishedOnce():
        if request._finishCalled == 1:
            return True
        else:
            raise AssertionError("request.finish was called %s times, "
                "expected 1" % (request._finishCalled,))

    request.finish = finish
    request.write = write
    request.assertWritten = assertWritten
    request.assertWrittenOnceWith = assertWrittenOnceWith
    request.assertFinishedOnce = assertFinishedOnce

    request.registerProducer = registerProducer
    request.unregisterProducer = unregisterProducer

    request.processingFailed = Mock(wraps=request.processingFailed)

    return request


def _render(resource, request):
    result = resource.render(request)

    if isinstance(result, str):
        request.write(result)
        request.finish()
        return succeed(None)
    elif result is server.NOT_DONE_YET:
        if request.finished:
            return succeed(None)
        else:
            return request.notifyFinish()
    else:
        raise ValueError("Unexpected return value: %r" % (result,))


class SimpleElement(Element):
    loader = XMLString("""
    <h1 xmlns:t="http://twistedmatrix.com/ns/twisted.web.template/0.1" t:render="name" />
    """)

    def __init__(self, name):
        self._name = name

    @renderer
    def name(self, request, tag):
        return tag(self._name)


class LeafResource(Resource):
    isLeaf = True

    def render(self, request):
        return "I am a leaf in the wind."


class ChildResource(Resource):
    isLeaf = True

    def __init__(self, name):
        self._name = name

    def render(self, request):
        return "I'm a child named %s!" % (self._name,)


class ChildrenResource(Resource):
    def render(self, request):
        return "I have children!"

    def getChild(self, path, request):
        if path == '':
            return self

        return ChildResource(path)


class ProducingResource(Resource):

    def __init__(self, path):
        self.path = path

    def render_GET(self, request):

        producer = MockProducer(request)
        producer.start()
        return server.NOT_DONE_YET


class MockProducer(object):

    def __init__(self, request):
        self.request = request
        self.count = 0

    def start(self):
        self.request.registerProducer(self, False)

    def resumeProducing(self):
        self.count += 1
        if self.count < 3:
            self.request.write("test")
        else:
            self.request.unregisterProducer()
            self.request.finish()


class KleinResourceTests(unittest.TestCase):
    def setUp(self):
        self.app = Klein()
        self.kr = KleinResource(self.app)


    def test_simplePost(self):
        app = self.app

        # The order in which these functions are defined
        # matters.  If the more generic one is defined first
        # then it will eat requests that should have been handled
        # by the more specific handler.

        @app.route("/", methods=["POST"])
        def handle_post(request):
            return 'posted'

        @app.route("/")
        def handle(request):
            return 'gotted'

        request = requestMock('/', 'POST')
        request2 = requestMock('/')

        d = _render(self.kr, request)

        def _cb(result):
            request.assertWritten('posted')
            return _render(self.kr, request2)

        d.addCallback(_cb)

        def _cb2(result):
            request2.assertWritten('gotted')

        d.addCallback(_cb2)
        return d


    def test_simpleRouting(self):
        app = self.app

        @app.route("/")
        def slash(request):
            return 'ok'

        request = requestMock('/')

        d = _render(self.kr, request)

        def _cb(result):
            request.assertWritten('ok')

        d.addCallback(_cb)

        return d


    def test_branchRendering(self):
        app = self.app

        @app.route("/", branch=True)
        def slash(request):
            return 'ok'

        request = requestMock('/foo')

        d = _render(self.kr, request)

        def _cb(result):
            request.assertWritten('ok')

        d.addCallback(_cb)

        return d


    def test_branchWithExplicitChildrenRouting(self):
        app = self.app

        @app.route("/")
        def slash(request):
            return 'ok'

        @app.route("/zeus")
        def wooo(request):
            return 'zeus'

        request = requestMock('/zeus')
        request2 = requestMock('/')

        d = _render(self.kr, request)

        def _cb(result):
            request.assertWritten('zeus')
            return _render(self.kr, request2)

        d.addCallback(_cb)

        def _cb2(result):
            request2.assertWritten('ok')

        d.addCallback(_cb2)

        return d


    def test_branchWithExplicitChildBranch(self):
        app = self.app

        @app.route("/", branch=True)
        def slash(request):
            return 'ok'

        @app.route("/zeus/", branch=True)
        def wooo(request):
            return 'zeus'

        request = requestMock('/zeus/foo')
        request2 = requestMock('/')

        d = _render(self.kr, request)

        def _cb(result):
            request.assertWritten('zeus')
            return _render(self.kr, request2)

        d.addCallback(_cb)

        def _cb2(result):
            request2.assertWritten('ok')

        d.addCallback(_cb2)

        return d


    def test_deferredRendering(self):
        app = self.app

        deferredResponse = Deferred()

        @app.route("/deferred")
        def deferred(request):
            return deferredResponse

        request = requestMock("/deferred")

        d = _render(self.kr, request)

        def _cb(result):
            request.assertWritten('ok')

        d.addCallback(_cb)
        deferredResponse.callback('ok')

        return d


    def test_elementRendering(self):
        app = self.app

        @app.route("/element/<string:name>")
        def element(request, name):
            return SimpleElement(name)

        request = requestMock("/element/foo")

        d = _render(self.kr, request)

        def _cb(result):
            request.assertWritten("<h1>foo</h1>")

        d.addCallback(_cb)

        return d


    def test_leafResourceRendering(self):
        app = self.app

        request = requestMock("/resource/leaf")

        @app.route("/resource/leaf")
        def leaf(request):
            return LeafResource()

        d = _render(self.kr, request)

        def _cb(result):
            request.assertWritten("I am a leaf in the wind.")

        d.addCallback(_cb)

        return d


    def test_childResourceRendering(self):
        app = self.app
        request = requestMock("/resource/children/betty")

        @app.route("/resource/children/", branch=True)
        def children(request):
            return ChildrenResource()

        d = _render(self.kr, request)

        def _cb(result):
            request.assertWritten("I'm a child named betty!")

        d.addCallback(_cb)

        return d


    def test_childrenResourceRendering(self):
        app = self.app

        request = requestMock("/resource/children/")

        @app.route("/resource/children/", branch=True)
        def children(request):
            return ChildrenResource()

        d = _render(self.kr, request)

        def _cb(result):
            request.assertWritten("I have children!")

        d.addCallback(_cb)

        return d


    def test_producerResourceRendering(self):
        app = self.app

        request = requestMock("/resource")

        @app.route("/resource", branch=True)
        def producer(request):
            return ProducingResource(request)

        d = _render(self.kr, request)

        def _cb(result):
            request.assertWritten("testtest")
            self.assertEqual(request.producer, None)

        d.addCallback(_cb)

        return d


    def test_notFound(self):
        request = requestMock("/fourohofour")

        d = _render(self.kr, request)

        def _cb(result):
            request.setResponseCode.assert_called_with(404)
            self.assertIn("404 Not Found",
                request._written.getvalue())

        d.addCallback(_cb)
        return d


    def test_renderUnicode(self):
        app = self.app

        request = requestMock("/snowman")

        @app.route("/snowman")
        def snowman(request):
            return u'\u2603'

        d = _render(self.kr, request)

        def _cb(result):
            request.assertWritten("\xE2\x98\x83")

        d.addCallback(_cb)
        return d


    def test_renderNone(self):
        app = self.app

        request = requestMock("/None")

        @app.route("/None")
        def none(request):
            return None

        d = _render(self.kr, request)

        def _cb(result):
            request.assertWrittenOnceWith('')
            request.assertFinishedOnce()

        d.addCallback(_cb)
        return d


    def test_staticRoot(self):
        app = self.app
        request = requestMock("/__init__.py")

        @app.route("/", branch=True)
        def root(request):
            return File(os.path.dirname(__file__))

        d = _render(self.kr, request)

        def _cb(result):
            request.assertWrittenOnceWith(
                open(
                    os.path.join(
                        os.path.dirname(__file__), "__init__.py")).read())
            request.assertFinishedOnce()

        d.addCallback(_cb)
        return d


    def test_explicitStaticBranch(self):
        app = self.app

        request = requestMock("/static/__init__.py")

        @app.route("/static/", branch=True)
        def root(request):
            return File(os.path.dirname(__file__))

        d = _render(self.kr, request)

        def _cb(result):
            request.assertWrittenOnceWith(
                open(
                    os.path.join(
                        os.path.dirname(__file__), "__init__.py")).read())
            request.assertFinishedOnce()

        d.addCallback(_cb)
        return d

    def test_staticDirlist(self):
        app = self.app

        request = requestMock("/")

        @app.route("/", branch=True)
        def root(request):
            return File(os.path.dirname(__file__))

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(request._writeCalled, 1)
            self.assertIn('Directory listing', request._written.getvalue())
            request.assertFinishedOnce()

        d.addCallback(_cb)
        return d

    def test_addSlash(self):
        app = self.app
        request = requestMock("/foo")

        @app.route("/foo/")
        def foo(request):
            return "foo"

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(request.setHeader.call_count, 3)
            request.setHeader.assert_has_calls(
                [call('Content-Type', 'text/html; charset=utf-8'),
                 call('Content-Length', '259'),
                 call('Location', 'http://localhost:8080/foo/')])

        d.addCallback(_cb)
        return d

    def test_methodNotAllowed(self):
        app = self.app
        request = requestMock("/foo", method='DELETE')

        @app.route("/foo", methods=['GET'])
        def foo(request):
            return "foo"

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(request.code, 405)

        d.addCallback(_cb)
        return d

    def test_methodNotAllowedWithRootCollection(self):
        app = self.app
        request = requestMock("/foo/bar", method='DELETE')

        @app.route("/foo/bar", methods=['GET'])
        def foobar(request):
            return "foo/bar"

        @app.route("/foo/", methods=['DELETE'])
        def foo(request):
            return "foo"

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(request.code, 405)

        d.addCallback(_cb)
        return d

    def test_noImplicitBranch(self):
        app = self.app
        request = requestMock("/foo")

        @app.route("/")
        def root(request):
            return "foo"

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(request.code, 404)

        d.addCallback(_cb)
        return d

    def test_strictSlashes(self):
        app = self.app
        request = requestMock("/foo/bar")

        request_url = [None]

        @app.route("/foo/bar/", strict_slashes=False)
        def root(request):
            request_url[0] = request.URLPath()
            return "foo"

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(str(request_url[0]), "http://localhost:8080/foo/bar")
            request.assertWritten('foo')
            self.assertEqual(request.code, 200)

        d.addCallback(_cb)
        return d

    def test_URLPath(self):
        app = self.app
        request = requestMock('/egg/chicken')

        request_url = [None]

        @app.route("/egg/chicken")
        def wooo(request):
            request_url[0] = request.URLPath()
            return 'foo'

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(str(request_url[0]), 'http://localhost:8080/egg/chicken')

        d.addCallback(_cb)
        return d

    def test_URLPath_root(self):
        app = self.app
        request = requestMock('/')

        request_url = [None]

        @app.route("/")
        def root(request):
            request_url[0] = request.URLPath()

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(str(request_url[0]), 'http://localhost:8080/')

        d.addCallback(_cb)
        return d

    def test_URLPath_traversedResource(self):
        app = self.app
        request = requestMock('/resource/foo')

        request_url = [None]

        class URLPathResource(Resource):
            def render(self, request):
                request_url[0] = request.URLPath()

            def getChild(self, request, segment):
                return self

        @app.route("/resource/", branch=True)
        def root(request):
            return URLPathResource()

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(str(request_url[0]), 'http://localhost:8080/resource/foo')

        d.addCallback(_cb)
        return d

    def test_handlerRaises(self):
        app = self.app
        request = requestMock("/")

        failures = []

        class RouteFailureTest(Exception):
            pass

        @app.route("/")
        def root(request):
            def _capture_failure(f):
                failures.append(f)
                return f

            return fail(RouteFailureTest("die")).addErrback(_capture_failure)

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(request.code, 500)
            request.processingFailed.assert_called_once_with(failures[0])
            self.flushLoggedErrors(RouteFailureTest)

        d.addCallback(_cb)
        return d

    def test_genericErrorHandler(self):
        app = self.app
        request = requestMock("/")

        failures = []

        class RouteFailureTest(Exception):
            pass

        @app.route("/")
        def root(request):
            raise RouteFailureTest("not implemented")

        @app.handle_errors
        def handle_errors(request, failure):
            failures.append(failure)
            request.setResponseCode(501)
            return

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(request.code, 501)
            assert not request.processingFailed.called

        d.addCallback(_cb)
        return d

    def test_typeSpecificErrorHandlers(self):
        app = self.app
        request = requestMock("/")
        type_error_handled = False
        generic_error_handled = False

        failures = []

        class TypeFilterTestError(Exception):
            pass

        @app.route("/")
        def root(request):
            return fail(TypeFilterTestError("not implemented"))

        @app.handle_errors(TypeError)
        def handle_type_error(request, failure):
            type_error_handled = True
            return

        @app.handle_errors(TypeFilterTestError)
        def handle_type_filter_test_error(request, failure):
            failures.append(failure)
            request.setResponseCode(501)
            return

        @app.handle_errors
        def handle_generic_error(request, failure):
            generic_error_handled = True
            return

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(request.processingFailed.called, False)
            self.assertEqual(type_error_handled, False)
            self.assertEqual(generic_error_handled, False)
            self.assertEqual(len(failures), 1)
            self.assertEqual(request.code, 501)

        d.addCallback(_cb)
        return d

    def test_notFoundException(self):
        app = self.app
        request = requestMock("/foo")
        generic_error_handled = False

        @app.route("/")
        def root(request):
            pass

        @app.handle_errors(NotFound)
        def handle_not_found(request, failure):
            request.setResponseCode(404)
            return 'Custom Not Found'

        @app.handle_errors
        def handle_generic_error(request, failure):
            generic_error_handled = True
            return

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(request.processingFailed.called, False)
            self.assertEqual(generic_error_handled, False)
            self.assertEqual(request.code, 404)
            request.assertWrittenOnceWith('Custom Not Found')

        d.addCallback(_cb)
        return d

    def test_requestWriteAfterFinish(self):
        app = self.app
        request = requestMock("/")

        @app.route("/")
        def root(request):
            request.finish()
            return 'foo'

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(request._writeCalled, 2)
            request.assertWritten('')
            [failure] = self.flushLoggedErrors(RuntimeError)

            self.assertEqual(
                str(failure.value),
                ("Request.write called on a request after Request.finish was "
                 "called."))

        d.addCallback(_cb)
        return d

    def test_requestFinishAfterConnectionLost(self):
        app = self.app
        request = requestMock("/")

        finished = Deferred()

        @app.route("/")
        def root(request):
            request.notifyFinish().addBoth(lambda _: finished.callback('foo'))
            return finished

        d = _render(self.kr, request)

        def _eb(result):
            [failure] = self.flushLoggedErrors(RuntimeError)

            self.assertEqual(
                str(failure.value),
                ("Request.finish called on a request after its connection was "
                 "lost; use Request.notifyFinish to keep track of this."))

        d.addErrback(lambda _: finished)
        d.addErrback(_eb)

        request.connectionLost(ConnectionLost())

        return d

    def test_routeHandlesRequestFinished(self):
        app = self.app
        request = requestMock("/")

        cancelled = []

        @app.route("/")
        def root(request):
            _d = Deferred()
            _d.addErrback(cancelled.append)
            request.notifyFinish().addCallback(lambda _: _d.cancel())
            return _d

        d = _render(self.kr, request)

        request.finish()

        def _cb(result):
            cancelled[0].trap(CancelledError)
            request.assertWrittenOnceWith('')
            self.assertEqual(request.processingFailed.call_count, 0)

        d.addCallback(_cb)
        return d

    def test_url_for(self):
        app = self.app
        request = requestMock('/foo/1')

        relative_url = [None]

        @app.route("/foo/<int:bar>")
        def foo(request, bar):
            krequest = IKleinRequest(request)
            relative_url[0] = krequest.url_for('foo', {'bar': bar + 1})

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(relative_url[0], '/foo/2')

        d.addCallback(_cb)
        return d

    def test_cancelledDeferred(self):
        app = self.app
        request = requestMock("/")

        inner_d = Deferred()

        @app.route("/")
        def root(request):
            return inner_d

        d = _render(self.kr, request)

        inner_d.cancel()

        def _cb(result):
            self.assertIdentical(result, None)
            self.flushLoggedErrors(CancelledError)

        d.addCallback(_cb)
        return d

    def test_external_url_for(self):
        app = self.app
        request = requestMock('/foo/1')

        relative_url = [None]

        @app.route("/foo/<int:bar>")
        def foo(request, bar):
            krequest = IKleinRequest(request)
            relative_url[0] = krequest.url_for('foo', {'bar': bar + 1}, force_external=True)

        d = _render(self.kr, request)

        def _cb(result):
            self.assertEqual(relative_url[0], 'http://localhost:8080/foo/2')

        d.addCallback(_cb)
        return d

    def test_cancelledIsEatenOnConnectionLost(self):
        app = self.app
        request = requestMock("/")

        @app.route("/")
        def root(request):
            _d = Deferred()
            request.notifyFinish().addErrback(lambda _: _d.cancel())
            return _d

        d = _render(self.kr, request)

        request.connectionLost(ConnectionLost())

        def _cb(result):
            self.assertEqual(request.processingFailed.call_count, 0)

        d.addErrback(lambda f: f.trap(ConnectionLost))
        d.addCallback(_cb)
        return d

    def test_cancelsOnConnectionLost(self):
        app = self.app
        request = requestMock("/")

        handler_d = Deferred()

        @app.route("/")
        def root(request):
            return handler_d

        d = _render(self.kr, request)
        request.connectionLost(ConnectionLost())

        handler_d.addErrback(lambda f: f.trap(CancelledError))

        d.addErrback(lambda f: f.trap(ConnectionLost))
        d.addCallback(lambda _: handler_d)

        return d

    def test_ensure_utf8_bytes(self):
        self.assertEqual(ensure_utf8_bytes(u"abc"), "abc")
        self.assertEqual(ensure_utf8_bytes(u"\u2202"), "\xe2\x88\x82")
        self.assertEqual(ensure_utf8_bytes("\xe2\x88\x82"), "\xe2\x88\x82")
