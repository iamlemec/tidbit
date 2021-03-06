import os
import sys
import re
import json
import argparse
import traceback
import operator as op

import tornado.ioloop
import tornado.web
import tornado.websocket
import tidbit as tb

# options
block_size = 25 # result chunk size

# utils
tagsort = lambda x: sorted(x,key=str.lower)
quotes = re.compile(r'\"([^\"]*)\"')

# parse input arguments
parser = argparse.ArgumentParser(description='Tidbit Server.')
parser.add_argument('db_fname', type=str, help='filename of database')
parser.add_argument('--port', type=int, default=9000, help='port to serve on')
args = parser.parse_args()

# initialize/open database
con = tb.connect(args.db_fname)

# code generation
results_str = """
{% for tb in results %}
  <div class="tb_box fresh" tid="{{ tb.id }}" selected="false" modified="false">
    <div class="tb_header">
      <span class="tb_title" contentEditable="true">{{ tb.title }}</span>
      <span class="tb_tags">
      {% for tag in sorted(tb.tags,key=str.lower) %}
      <span class="tb_tag"><span class="nametag">{{ tag }}</span><span class="deltag">x</span></span>
      {% end %}
      </span>
      <span class="newtag">+</span>
    </div>
    <div class="tb_body" contentEditable="true">{% raw tb.body %}</div>
    <div class="revert control">Revert</div>
    <div class="save control">Save</div>
    <div class="delete control">x</div>
  </div>
{% end %}
"""
results_template = tornado.template.Template(results_str)

# password authentication
with open('auth.txt') as fid:
    auth = json.load(fid)
cookie_secret = auth['cookie_secret']
username_true = auth['username']
password_true = auth['password']

def authenticated(get0):
    def get1(self,*args):
        current_user = self.get_secure_cookie("user")
        print(current_user)
        if not current_user:
            self.redirect("/auth/login/")
            return
        get0(self,*args)
    return get1

# tornado time
class AuthLoginHandler(tornado.web.RequestHandler):
    def get(self):
        try:
            errormessage = self.get_argument("error")
        except:
            errormessage = ""
        self.render("login.html",errormessage=errormessage)

    def check_permission(self, password, username):
        if username == username_true and password == password_true:
            return True
        return False

    def post(self):
        username = self.get_argument("username", "")
        password = self.get_argument("password", "")
        auth = self.check_permission(password,username)
        if auth:
            self.set_current_user(username)
            self.redirect("/")
        else:
            error_msg = "?error=" + tornado.escape.url_escape("Login incorrect")
            self.redirect("/auth/login/" + error_msg)

    def set_current_user(self, user):
        if user:
            print(user)
            self.set_secure_cookie("user",tornado.escape.json_encode(user))
        else:
            self.clear_cookie("user")

class AuthLogoutHandler(tornado.web.RequestHandler):
    def get(self):
        self.clear_cookie("user")
        self.redirect(self.get_argument("next","/"))

class EditorHandler(tornado.web.RequestHandler):
    @authenticated
    def get(self):
        self.render("editor.html")

class TidbitHandler(tornado.websocket.WebSocketHandler):
    def initialize(self):
        print("initializing")
        self.results = []

    def allow_draft76(self):
        return True

    def open(self):
        print("connection received")

    def on_close(self):
        print("connection closing")

    def error_msg(self, error_code):
        if not error_code is None:
            json_string = json.dumps({"type": "error", "code": error_code})
            self.write_message("{0}".format(json_string))
        else:
            print("error code not found")

    def on_message(self, msg):
        try:
            print(u'received message: {0}'.format(msg))
        except Exception as e:
            print(e)
        data = json.loads(msg)
        (cmd,cont) = (data['cmd'],data['content'])
        if cmd == 'query':
            try:
                if cont == '':
                    id_list = con.search('')
                else:
                    # generate terms list
                    lastidx = 0
                    remain = ''
                    terms = []
                    for chunk in quotes.finditer(cont):
                        (i1,i2) = chunk.span()
                        remain += cont[lastidx:i1]
                        terms.append(cont[i1+1:i2-1])
                        lastidx = i2
                    remain += cont[lastidx:]
                    terms += remain.split()

                    # fetch related ids
                    id_sets = []
                    for term in terms:
                        if term.startswith('#'):
                            ids = con.find_tag(term[1:])
                        else:
                            ids = con.search(term)
                        id_sets.append(set(ids))
                    id_list = list(set.intersection(*id_sets))

                # get text
                tds = [con.get_by_id(id) for id in id_list]
                tsort = sorted(tds,key=op.attrgetter('timestamp','id'),reverse=True)
                size = min(len(tsort),block_size)
                (first,self.results) = (tsort[:size],tsort[size:])
                reset = True
                done = (len(self.results) == 0)
                gen = results_template.generate(results=first).decode()
            except Exception as e:
                print(e)
                print(traceback.format_exc())
                reset = True
                done = True
                gen = '<div class="error">Error</div>'
            self.write_message(json.dumps({'cmd': 'results', 'content': {'reset': reset, 'done': done, 'html': gen}}))
        elif cmd == 'moar':
            size = min(len(self.results),block_size)
            (block,self.results) = (self.results[:size],self.results[size:])
            reset = False
            done = (len(self.results) == 0)
            gen = results_template.generate(results=block).decode()
            self.write_message(json.dumps({'cmd': 'results', 'content': {'reset': reset, 'done': done, 'html': gen}}))
        elif cmd == 'set':
            try:
                oldid = cont['tid']
                id = None if oldid == 'new' else oldid
                tid = tb.Tidbit(id=id,title=cont['title'],body=cont['body'],tags=cont['tags'])
                con.save(tid)
                self.write_message(json.dumps({'cmd': 'success', 'content': {'oldid': oldid, 'newid': tid.id}}))
            except Exception as e:
                print(e)
                print(traceback.format_exc())
        elif cmd == 'get':
            try:
                tid = con.get_by_id(cont)
                if tid:
                    gen = results_template.generate(results=[tid]).decode()
                    self.write_message(json.dumps({'cmd': 'set', 'content': {'id': cont, 'box': gen}}))
            except Exception as e:
                print(e)
        elif cmd == 'new':
            try:
                title = cont if cont != '' else 'Title'
                tid = tb.Tidbit()
                tid.id = 'new'
                tid.set_title(title)
                tid.set_body('')
                gen = results_template.generate(results=[tid]).decode()
                self.write_message(json.dumps({'cmd': 'new', 'content': gen}))
            except Exception as e:
                print(e)
        elif cmd == 'delete':
            try:
                con.delete_id(cont)
                self.write_message(json.dumps({'cmd': 'remove', 'content': {'id': cont}}))
            except Exception as e:
                print(e)

# tornado content handlers
class Application(tornado.web.Application):
    def __init__(self):
        handlers = [
            (r"/auth/login/?", AuthLoginHandler),
            (r"/auth/logout/?", AuthLogoutHandler),
            (r"/", EditorHandler),
            (r"/tidbit", TidbitHandler)
        ]
        settings = dict(
            app_name=u"Tidbit Editor",
            template_path="templates",
            static_path="static",
            cookie_secret=cookie_secret
        )
        tornado.web.Application.__init__(self, handlers, debug=True, **settings)

# create server
application = Application()
application.listen(args.port)
tornado.ioloop.IOLoop.current().start()
