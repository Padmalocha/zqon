from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_socketio import SocketIO, emit, join_room
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import pytz

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-key-12345-change-this-in-production'

app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://root:@localhost/chatapp'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", logger=True)

IST = pytz.timezone('Asia/Kolkata')

online_users = {}


# ====================== MODELS ======================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    last_seen = db.Column(db.DateTime)


class Contact(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    contact_username = db.Column(db.String(80), nullable=False)
    display_name = db.Column(db.String(80), nullable=False)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender = db.Column(db.String(80), nullable=False)
    room = db.Column(db.String(100), nullable=False)
    message = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    seen = db.Column(db.Boolean, default=False)


with app.app_context():
    db.create_all()


# ====================== ROUTES ======================
@app.route('/')
def home():
    return render_template('index.html')


@app.route('/register', methods=['GET', 'POST'])
def register_page():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        if User.query.filter_by(username=username).first():
            flash('Username already exists!', 'error')
        else:
            new_user = User(username=username, password=generate_password_hash(password))
            db.session.add(new_user)
            db.session.commit()
            flash('Registration successful! Please login.', 'success')
            return redirect(url_for('login_page'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session['username'] = username
            return redirect(url_for('contacts_page'))
        flash('Invalid username or password!', 'error')
    return render_template('login.html')


@app.route('/contacts')
def contacts_page():
    if 'username' not in session:
        return redirect(url_for('login_page'))

    current_user = session['username']
    user_obj = User.query.filter_by(username=current_user).first()
    contacts = Contact.query.filter_by(user_id=user_obj.id).all()

    online_status = {
        c.contact_username: any(u == c.contact_username for u in online_users.values())
        for c in contacts
    }

    return render_template('contacts.html',
                           contacts=contacts,
                           current_user=current_user,
                           online_status=online_status)


# ==================== ADD CONTACT ====================
@app.route('/add_contact', methods=['POST'])
def add_contact():
    if 'username' not in session:
        return redirect(url_for('login_page'))

    contact_username = request.form['contact_username'].strip()
    display_name = request.form.get('display_name', '').strip() or contact_username

    if contact_username == session['username']:
        flash("You can't add yourself!", 'error')
        return redirect(url_for('contacts_page'))

    if not User.query.filter_by(username=contact_username).first():
        flash(f'User "{contact_username}" not found!', 'error')
        return redirect(url_for('contacts_page'))

    current_user_obj = User.query.filter_by(username=session['username']).first()

    if Contact.query.filter_by(user_id=current_user_obj.id, contact_username=contact_username).first():
        flash(f'"{contact_username}" is already in contacts!', 'error')
    else:
        new_contact = Contact(
            user_id=current_user_obj.id,
            contact_username=contact_username,
            display_name=display_name
        )
        db.session.add(new_contact)
        db.session.commit()
        flash(f'"{display_name}" added successfully!', 'success')

    return redirect(url_for('contacts_page'))


@app.route('/chat/<target>')
def private_chat(target):
    if 'username' not in session:
        return redirect(url_for('login_page'))

    user_obj = User.query.filter_by(username=session['username']).first()
    contact = Contact.query.filter_by(user_id=user_obj.id, contact_username=target).first()

    if not contact:
        flash("You can only chat with saved contacts!", 'error')
        return redirect(url_for('contacts_page'))

    room = f"private_{min(session['username'], target)}_{max(session['username'], target)}"

    target_user = User.query.filter_by(username=target).first()
    is_online = any(u == target for u in online_users.values())

    last_seen = ""
    if target_user and target_user.last_seen:
        last_seen = target_user.last_seen.replace(tzinfo=pytz.utc).astimezone(IST).strftime('%I:%M %p')

    return render_template('chat.html',
                           target=target,
                           display_name=contact.display_name,
                           room=room,
                           is_online=is_online,
                           last_seen=last_seen)


@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('login_page'))


# ====================== SOCKET EVENTS ======================

@socketio.on('connect')
def handle_connect():
    username = session.get('username')
    if username:
        online_users[request.sid] = username
        emit('user_status', {'username': username, 'status': 'online'}, broadcast=True)


@socketio.on('disconnect')
def handle_disconnect():
    username = online_users.pop(request.sid, None)
    if username:
        user = User.query.filter_by(username=username).first()
        if user:
            user.last_seen = datetime.utcnow()
            db.session.commit()

        emit('user_status', {
            'username': username,
            'status': 'offline',
            'last_seen': datetime.now(IST).strftime('%I:%M %p')
        }, broadcast=True)


@socketio.on('join')
def on_join(data):
    room = data['room']
    join_room(room)

    username = session.get('username')
    msgs = Message.query.filter_by(room=room).order_by(Message.timestamp.asc()).all()

    # Mark as seen
    for m in msgs:
        if m.sender != username:
            m.seen = True
    db.session.commit()

    for m in msgs:
        local_time = m.timestamp.replace(tzinfo=pytz.utc).astimezone(IST)
        emit('message', {
            'username': m.sender,
            'msg': m.message,
            'time': local_time.strftime('%I:%M %p'),
            'seen': m.seen
        }, to=request.sid)


@socketio.on('message')
def handle_message(data):
    username = session.get('username')
    if not username:
        return

    room = data.get('room')
    msg_text = data.get('message')

    if not msg_text or not room:
        return

    new_msg = Message(sender=username, room=room, message=msg_text)
    db.session.add(new_msg)
    db.session.commit()

    emit('message', {
        'username': username,
        'msg': msg_text,
        'time': datetime.now(IST).strftime('%I:%M %p'),
        'seen': False
    }, room=room, broadcast=True)


@socketio.on('typing')
def handle_typing(data):
    emit('user_typing', {
        'username': session.get('username'),
        'is_typing': True
    }, room=data.get('room'), include_self=False)


@socketio.on('stop_typing')
def handle_stop_typing(data):
    emit('user_typing', {
        'username': session.get('username'),
        'is_typing': False
    }, room=data.get('room'), include_self=False)


# ====================== RUN ======================
if __name__ == '__main__':
    print("🚀 Live Chat App Running!")
    print("→ http://localhost:5000")
    socketio.run(app, debug=True, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)