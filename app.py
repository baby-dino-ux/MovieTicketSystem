from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import mysql.connector
import bcrypt, re, uuid
from functools import wraps
from datetime import datetime, date, timedelta

app = Flask(__name__)
app.secret_key = 'tickit_secret_key_2024'

# ── DB ─────────────────────────────────────────────────────────
def get_db():
    return mysql.connector.connect(
        host='localhost', port=3306,
        user='root', password='2006',
        database='tickit_db',
        autocommit=False
    )

# ── HELPERS ────────────────────────────────────────────────────
def is_valid_email(v):  return re.match(r'^[\w\.-]+@[\w\.-]+\.\w{2,}$', v)
def is_valid_phone(v):  return re.match(r'^(\+63|0)\d{10}$', v)

TICKET_PRICES = {'Regular': 450, 'Student': 350, 'Senior / PWD': 360}

def fmt_time(raw_t):
    """Format MySQL TIME (timedelta or time object) to '2:30 PM'."""
    if hasattr(raw_t, 'strftime'):
        return raw_t.strftime('%I:%M %p').lstrip('0')
    total_secs = int(raw_t.total_seconds())
    hrs  = total_secs // 3600
    mins = (total_secs % 3600) // 60
    suffix = 'AM' if hrs < 12 else 'PM'
    hrs12  = hrs % 12 or 12
    return f'{hrs12}:{mins:02d} {suffix}'

# ── AUTH DECORATOR ─────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in first.', 'warning')
            return redirect(url_for('login'))
        try:
            db  = get_db()
            cur = db.cursor()
            cur.execute("SELECT id FROM users WHERE id=%s", (session['user_id'],))
            exists = cur.fetchone()
            cur.close(); db.close()
            if not exists:
                session.clear()
                flash('Your session has expired. Please log in again.', 'warning')
                return redirect(url_for('login'))
        except Exception:
            pass
        return f(*args, **kwargs)
    return decorated

# ── MAINTENANCE ────────────────────────────────────────────────
def run_maintenance(db):
    cur = db.cursor()
    cur.execute("""
        UPDATE showings SET status='completed'
         WHERE status IN ('open','scheduled','full')
           AND TIMESTAMP(show_date, show_time) < NOW()
    """)
    cur.execute("""
        UPDATE seats SET status='available', locked_until=NULL
         WHERE status='locked' AND locked_until < NOW()
    """)
    cur.execute("""
        UPDATE bookings b JOIN showings s ON s.id=b.showing_id
           SET b.status='Completed'
         WHERE s.status='completed' AND b.status='Confirmed'
    """)
    db.commit()
    cur.close()

# ── ENSURE SEATS ───────────────────────────────────────────────
def ensure_seats(db, showing_id):
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM seats WHERE showing_id=%s", (showing_id,))
    if cur.fetchone()[0] == 0:
        cur.execute("CALL seed_seats(%s)", (showing_id,))
        db.commit()
    cur.close()

# ── ROUTES ─────────────────────────────────────────────────────
@app.route('/')
def landing():
    if 'user_id' in session:
        return redirect(url_for('index'))
    return render_template('landing.html')

@app.route('/home')
@login_required
def index():
    return render_template('index.html', user_name=session.get('user_name'))

@app.route('/movies')
@login_required
def movies():
    return render_template('movies.html', user_name=session.get('user_name'))

# ── BOOKING (3-step wizard) ────────────────────────────────────
@app.route('/booking')
@login_required
def booking():
    db = get_db()
    run_maintenance(db)

    movie_id   = request.args.get('movie_id',   type=int)
    showing_id = request.args.get('showing_id', type=int)

    # If no movie selected, redirect to movies page to pick one
    if not movie_id and not showing_id:
        db.close()
        flash('Please select a movie to book.', 'info')
        return redirect(url_for('movies'))

    cur = db.cursor(dictionary=True)

    # ── Step 1: All active movies (kept for template compatibility) ─
    all_movies = []

    selected_movie   = None
    showings_by_date = {}
    selected_showing = None
    seat_rows        = []

    # ── Step 2: Showings for selected movie ───────────────────
    if movie_id:
        cur.execute("SELECT * FROM movies WHERE id=%s AND status='active'", (movie_id,))
        selected_movie = cur.fetchone()

        if selected_movie:
            cur.execute("""
                SELECT s.id, s.show_date, s.show_time, s.status, s.total_seats,
                       c.name AS cinema_name, c.location AS cinema_location,
                       COALESCE((SELECT COUNT(*) FROM seats st
                                  WHERE st.showing_id=s.id AND st.status='booked'),0) AS booked_count,
                       COALESCE((SELECT COUNT(*) FROM seats st
                                  WHERE st.showing_id=s.id AND st.status='available'),0) AS avail_count,
                       COALESCE((SELECT COUNT(*) FROM seats st
                                  WHERE st.showing_id=s.id),0) AS total_seeded
                FROM showings s
                JOIN cinemas c ON c.id=s.cinema_id
                WHERE s.movie_id=%s
                  AND s.status IN ('open','scheduled','full')
                  AND TIMESTAMP(s.show_date, s.show_time) > NOW()
                  AND s.show_date <= DATE_ADD(CURDATE(), INTERVAL 7 DAY)
                ORDER BY s.show_date, s.show_time
            """, (movie_id,))
            raw_showings = cur.fetchall()

            for sh in raw_showings:
                if sh['total_seeded'] == 0:
                    ensure_seats(db, sh['id'])
                    sh['avail_count'] = 50
                if sh['avail_count'] == 0 and sh['booked_count'] == 0:
                    sh['avail_count'] = sh['total_seats']

                d_str   = sh['show_date'].strftime('%Y-%m-%d')
                d_label = sh['show_date'].strftime('%A, %B %d %Y')
                if d_str not in showings_by_date:
                    showings_by_date[d_str] = {'label': d_label, 'showings': []}

                avail = sh['avail_count']
                if avail == 0:
                    sh['avail_label'] = 'SOLD OUT'
                    sh['avail_class'] = 'full'
                elif avail <= 8:
                    sh['avail_label'] = f'Only {avail} left!'
                    sh['avail_class'] = 'low'
                else:
                    sh['avail_label'] = f'{avail} of {sh["total_seats"]} available'
                    sh['avail_class'] = 'ok'

                sh['show_time_fmt'] = fmt_time(sh['show_time'])
                showings_by_date[d_str]['showings'].append(sh)

    # ── Step 3: Seat map ──────────────────────────────────────
    if showing_id:
        ensure_seats(db, showing_id)
        cur.execute("""
            SELECT s.id, s.show_date, s.show_time, s.status AS show_status,
                   s.total_seats,
                   c.name AS cinema_name, c.location AS cinema_location,
                   m.title AS movie_title, m.genre, m.rating,
                   m.poster_path, m.id AS movie_id_val
            FROM showings s
            JOIN cinemas c ON c.id=s.cinema_id
            JOIN movies  m ON m.id=s.movie_id
            WHERE s.id=%s
        """, (showing_id,))
        row = cur.fetchone()
        if row:
            selected_showing = row
            selected_showing['show_time_fmt'] = fmt_time(row['show_time'])
            selected_showing['show_date_fmt'] = row['show_date'].strftime('%A, %B %d %Y')
            if not movie_id:
                movie_id = row['movie_id_val']
            if not selected_movie:
                selected_movie = {
                    'id': row['movie_id_val'], 'title': row['movie_title'],
                    'genre': row['genre'],     'rating': row['rating'],
                    'poster_path': row['poster_path'],
                }

        cur.execute("""
            SELECT id, row_label, seat_number, seat_code, category, status, locked_until
            FROM seats WHERE showing_id=%s
            ORDER BY row_label, seat_number
        """, (showing_id,))
        all_seats = cur.fetchall()

        from collections import defaultdict
        rows_dict = defaultdict(list)
        for s in all_seats:
            rows_dict[s['row_label']].append(s)
        seat_rows = [(lbl, rows_dict[lbl]) for lbl in sorted(rows_dict.keys())]

    cur.close(); db.close()

    return render_template('booking.html',
        user_name        = session.get('user_name'),
        all_movies       = all_movies,
        selected_movie   = selected_movie,
        movie_id         = movie_id,
        showings_by_date = showings_by_date,
        selected_showing = selected_showing,
        showing_id       = showing_id,
        seat_rows        = seat_rows,
        booking_success  = False,
        review_mode      = False,
        review_data      = None,
    )


# ── BOOKING REVIEW (Step 4 — payment confirmation) ─────────────
@app.route('/booking/review', methods=['POST'])
@login_required
def booking_review():
    """Show summary for user to confirm before finalising."""
    seat_ids_raw  = request.form.get('seat_ids', '').strip()
    showing_id    = request.form.get('showing_id', type=int)
    ticket_type   = request.form.get('ticket_type', 'Regular').strip()
    customer_name = request.form.get('customer_name', '').strip()
    contact       = request.form.get('contact', '').strip()
    special       = request.form.get('special_requests', '').strip()

    # Basic validation
    errors = []
    seat_ids = [int(x) for x in seat_ids_raw.split(',') if x.strip().isdigit()]
    if not seat_ids:               errors.append('Please select at least one seat.')
    if len(seat_ids) > 10:         errors.append('Maximum 10 seats per booking.')
    if not customer_name or len(customer_name) < 2:
                                   errors.append('Please enter your full name (min 2 characters).')
    if not re.match(r'^(\+63|0)\d{10}$', contact):
                                   errors.append('Enter a valid PH mobile: 09XXXXXXXXX or +639XXXXXXXXX.')
    if ticket_type not in TICKET_PRICES:
                                   errors.append('Invalid ticket type.')
    if errors:
        for e in errors:
            flash(e, 'error')
        return redirect(url_for('booking', showing_id=showing_id))

    db  = get_db()
    cur = db.cursor(dictionary=True)

    # Validate seats are still available
    unavailable = []
    for sid in seat_ids:
        cur.execute("SELECT seat_code, status FROM seats WHERE id=%s", (sid,))
        seat = cur.fetchone()
        if not seat or seat['status'] == 'booked':
            unavailable.append(seat['seat_code'] if seat else str(sid))

    if unavailable:
        cur.close(); db.close()
        flash(f'Seat(s) {", ".join(unavailable)} are no longer available. Please re-select.', 'error')
        return redirect(url_for('booking', showing_id=showing_id))

    # Build review data
    cur.execute("""
        SELECT s.show_date, s.show_time,
               c.name AS cinema, c.location AS cinema_location,
               m.title AS movie, m.poster_path, m.duration_mins, m.id AS movie_id
        FROM showings s
        JOIN cinemas c ON c.id=s.cinema_id
        JOIN movies  m ON m.id=s.movie_id
        WHERE s.id=%s
    """, (showing_id,))
    sh = cur.fetchone()

    cur.execute("""
        SELECT seat_code, category FROM seats WHERE id IN ({})
    """.format(','.join(['%s']*len(seat_ids))), seat_ids)
    seat_info = cur.fetchall()
    cur.close(); db.close()

    unit_price = TICKET_PRICES[ticket_type]
    review_data = {
        'movie':          sh['movie'],
        'poster_path':    sh['poster_path'],
        'cinema':         sh['cinema'],
        'cinema_location':sh['cinema_location'],
        'date':           sh['show_date'].strftime('%A, %B %d %Y'),
        'showtime':       fmt_time(sh['show_time']),
        'seat_info':      seat_info,
        'seats_display':  ', '.join(f"{s['seat_code']} ({s['category']})" for s in seat_info),
        'ticket_count':   len(seat_ids),
        'ticket_type':    ticket_type,
        'unit_price':     unit_price,
        'total_price':    f'{unit_price * len(seat_ids):,}',
        'customer_name':  customer_name,
        'contact':        contact,
        'special':        special,
        'showing_id':     showing_id,
        'movie_id':       sh['movie_id'],
        'seat_ids':       seat_ids_raw,
        'temp_ref':       'TKT-' + uuid.uuid4().hex[:8].upper(),
    }

    return render_template('booking.html',
        user_name        = session.get('user_name'),
        all_movies       = [],
        selected_movie   = None,
        movie_id         = None,
        showings_by_date = {},
        selected_showing = None,
        showing_id       = showing_id,
        seat_rows        = [],
        booking_success  = False,
        review_mode      = True,
        review_data      = review_data,
    )


# ── BOOKING CONFIRM (final — records to DB) ────────────────────
@app.route('/booking/confirm', methods=['POST'])
@login_required
def confirm_booking():
    seat_ids_raw    = request.form.get('seat_ids', '').strip()
    showing_id      = request.form.get('showing_id', type=int)
    ticket_type     = request.form.get('ticket_type', 'Regular').strip()
    customer_name   = request.form.get('customer_name', '').strip()
    contact         = request.form.get('contact', '').strip()
    special         = request.form.get('special_requests', '').strip()
    payment_method  = request.form.get('payment_method', 'counter').strip()
    payment_account = request.form.get('payment_account', '').strip()
    temp_ref        = request.form.get('temp_ref', '').strip()

    seat_ids = [int(x) for x in seat_ids_raw.split(',') if x.strip().isdigit()]

    if not seat_ids or not showing_id or ticket_type not in TICKET_PRICES:
        flash('Invalid booking data. Please start again.', 'error')
        return redirect(url_for('booking'))

    db  = get_db()
    cur = db.cursor(dictionary=True)

    try:
        # Guard: stale session
        cur.execute("SELECT id FROM users WHERE id=%s", (session['user_id'],))
        if not cur.fetchone():
            cur.close(); db.close()
            session.clear()
            flash('Your session has expired. Please log in again.', 'warning')
            return redirect(url_for('login'))

        # Validate showing
        cur.execute("SELECT * FROM showings WHERE id=%s FOR UPDATE", (showing_id,))
        showing = cur.fetchone()
        if not showing or showing['status'] not in ('open', 'scheduled', 'full'):
            flash('This showing is no longer available.', 'error')
            db.rollback(); cur.close(); db.close()
            return redirect(url_for('booking'))

        # Verify all seats still available
        for sid in seat_ids:
            cur.execute("SELECT * FROM seats WHERE id=%s FOR UPDATE", (sid,))
            seat = cur.fetchone()
            if not seat or seat['status'] == 'booked':
                code = seat['seat_code'] if seat else str(sid)
                flash(f'Seat {code} was just taken. Please re-select.', 'error')
                db.rollback(); cur.close(); db.close()
                return redirect(url_for('booking', showing_id=showing_id))

        unit_price = TICKET_PRICES[ticket_type]
        # Use the temp_ref from review page (so counter ref matches what user saw)
        ref_code = temp_ref if temp_ref else ('TKT-' + uuid.uuid4().hex[:8].upper())

        # Append payment info to special_requests for record
        pay_note = f'[Payment: {payment_method.upper()}'
        if payment_account:
            pay_note += f' — {payment_account}'
        pay_note += ']'
        full_special = f'{special} {pay_note}'.strip()

        for sid in seat_ids:
            cur.execute("UPDATE seats SET status='booked', locked_until=NULL WHERE id=%s", (sid,))
            cur.execute("""
                INSERT INTO bookings
                    (user_id, showing_id, seat_id, ticket_type, unit_price,
                     customer_name, contact, special_requests, ref_code)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (session['user_id'], showing_id, sid, ticket_type, unit_price,
                  customer_name, contact, full_special, ref_code))

        # Mark showing full if needed
        cur.execute("SELECT COUNT(*) AS avail FROM seats WHERE showing_id=%s AND status='available'", (showing_id,))
        if cur.fetchone()['avail'] == 0:
            cur.execute("UPDATE showings SET status='full' WHERE id=%s", (showing_id,))

        db.commit()

        # Build receipt
        cur.execute("""
            SELECT s.show_date, s.show_time, c.name AS cinema, m.title AS movie,
                   m.poster_path
            FROM showings s
            JOIN cinemas c ON c.id=s.cinema_id
            JOIN movies  m ON m.id=s.movie_id
            WHERE s.id=%s
        """, (showing_id,))
        sh = cur.fetchone()
        cur.execute("""
            SELECT seat_code, category FROM seats WHERE id IN ({})
        """.format(','.join(['%s']*len(seat_ids))), seat_ids)
        seat_info = cur.fetchall()
        cur.close(); db.close()

        booking_data = {
            'movie':           sh['movie'],
            'cinema':          sh['cinema'],
            'date':            sh['show_date'].strftime('%A, %B %d %Y'),
            'showtime':        fmt_time(sh['show_time']),
            'seats':           ', '.join(f"{s['seat_code']} ({s['category']})" for s in seat_info),
            'ticket_count':    len(seat_ids),
            'ticket_type':     ticket_type,
            'total_price':     f'{unit_price * len(seat_ids):,}',
            'ref':             ref_code,
            'payment_method':  payment_method,
            'payment_account': payment_account,
        }

        return render_template('booking.html',
            user_name        = session.get('user_name'),
            booking_success  = True,
            booking          = booking_data,
            all_movies=[], selected_movie=None, movie_id=None,
            showings_by_date={}, selected_showing=None, showing_id=None,
            seat_rows=[], review_mode=False, review_data=None,
        )

    except Exception as e:
        db.rollback()
        cur.close(); db.close()
        flash(f'Booking error: {str(e)}', 'error')
        return redirect(url_for('booking', showing_id=showing_id))


# ── API: LOCK SEAT ──────────────────────────────────────────────
@app.route('/api/lock-seat', methods=['POST'])
@login_required
def lock_seat():
    data       = request.get_json(force=True)
    seat_id    = data.get('seat_id')
    showing_id = data.get('showing_id')
    if not seat_id or not showing_id:
        return jsonify({'ok': False, 'msg': 'Missing params'})

    db  = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM seats WHERE id=%s FOR UPDATE", (seat_id,))
        seat = cur.fetchone()
        if not seat or seat['status'] != 'available':
            db.rollback(); cur.close(); db.close()
            return jsonify({'ok': False, 'msg': 'Seat no longer available'})
        lock_exp = datetime.now() + timedelta(minutes=5)
        cur.execute("UPDATE seats SET status='locked', locked_until=%s WHERE id=%s",
                    (lock_exp, seat_id))
        db.commit(); cur.close(); db.close()
        return jsonify({'ok': True, 'expires': lock_exp.strftime('%H:%M:%S')})
    except Exception as e:
        db.rollback(); cur.close(); db.close()
        return jsonify({'ok': False, 'msg': str(e)})


# ── API: UNLOCK SEAT ────────────────────────────────────────────
@app.route('/api/unlock-seat', methods=['POST'])
@login_required
def unlock_seat():
    data    = request.get_json(force=True)
    seat_id = data.get('seat_id')
    if not seat_id:
        return jsonify({'ok': False})
    db  = get_db()
    cur = db.cursor()
    cur.execute("UPDATE seats SET status='available', locked_until=NULL WHERE id=%s AND status='locked'",
                (seat_id,))
    db.commit(); cur.close(); db.close()
    return jsonify({'ok': True})


# ── API: SEAT STATUS ────────────────────────────────────────────
@app.route('/api/seat-status/<int:showing_id>')
@login_required
def seat_status(showing_id):
    db  = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("""
        UPDATE seats SET status='available', locked_until=NULL
         WHERE showing_id=%s AND status='locked' AND locked_until < NOW()
    """, (showing_id,))
    db.commit()
    cur.execute("""
        SELECT id, seat_code, status, category, row_label, seat_number
        FROM seats WHERE showing_id=%s ORDER BY row_label, seat_number
    """, (showing_id,))
    seats = cur.fetchall()
    cur.close(); db.close()
    return jsonify({'seats': seats})


# ── MY BOOKINGS ─────────────────────────────────────────────────
@app.route('/my-bookings')
@login_required
def my_bookings():
    db  = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("""
        SELECT b.ref_code, b.ticket_type, b.unit_price,
               b.status AS booking_status, b.created_at,
               b.customer_name, b.contact, b.special_requests,
               st.seat_code, st.category,
               m.title AS movie, m.poster_path,
               c.name AS cinema, c.location AS cinema_location,
               s.show_date, s.show_time
        FROM bookings b
        JOIN seats    st ON st.id = b.seat_id
        JOIN showings s  ON s.id  = b.showing_id
        JOIN movies   m  ON m.id  = s.movie_id
        JOIN cinemas  c  ON c.id  = s.cinema_id
        WHERE b.user_id = %s
        ORDER BY b.created_at DESC
    """, (session['user_id'],))
    rows = cur.fetchall()
    cur.close(); db.close()

    from collections import defaultdict
    grouped = defaultdict(list)
    for r in rows:
        grouped[r['ref_code']].append(r)

    bookings_list = []
    for ref, seats in grouped.items():
        first = seats[0]
        total = sum(s['unit_price'] for s in seats)
        bookings_list.append({
            'ref':             ref,
            'movie':           first['movie'],
            'poster_path':     first['poster_path'],
            'cinema':          first['cinema'],
            'cinema_location': first['cinema_location'],
            'date':            first['show_date'].strftime('%b %d, %Y'),
            'showtime':        fmt_time(first['show_time']),
            'seats':           ', '.join(s['seat_code'] for s in seats),
            'seat_info':       [{'code': s['seat_code'], 'cat': s['category']} for s in seats],
            'ticket_type':     first['ticket_type'],
            'total':           f'{total:,}',
            'status':          first['booking_status'],
            'booked_on':       first['created_at'].strftime('%b %d, %Y %I:%M %p'),
            'special':         first['special_requests'] or '',
        })

    return render_template('my_bookings.html',
        user_name=session.get('user_name'),
        bookings=bookings_list
    )


# ── LOGIN ───────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    errors = {}; form = {}
    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        password   = request.form.get('password',   '').strip()
        form = {'identifier': identifier}
        if not identifier:
            errors['identifier'] = 'Email or mobile is required.'
        elif not is_valid_email(identifier) and not is_valid_phone(identifier):
            errors['identifier'] = 'Enter a valid email or PH mobile (09XXXXXXXXX).'
        if not password:
            errors['password'] = 'Password is required.'
        elif len(password) < 6:
            errors['password'] = 'Min 6 characters.'
        if not errors:
            try:
                db  = get_db()
                cur = db.cursor(dictionary=True)
                cur.execute('SELECT * FROM users WHERE email=%s OR mobile=%s',
                            (identifier, identifier))
                user = cur.fetchone()
                cur.close(); db.close()
                if user and bcrypt.checkpw(password.encode(), user['password'].encode()):
                    session['user_id']   = user['id']
                    session['user_name'] = user['full_name']
                    return redirect(url_for('index'))
                else:
                    errors['general'] = 'Invalid credentials. Please try again.'
            except Exception as e:
                errors['general'] = f'Database error: {e}'
    return render_template('login.html', errors=errors, form=form)


# ── REGISTER ────────────────────────────────────────────────────
@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect(url_for('index'))
    errors = {}; form = {}
    if request.method == 'POST':
        identifier = request.form.get('identifier',       '').strip()
        full_name  = request.form.get('full_name',        '').strip()
        age        = request.form.get('age',              '').strip()
        gender     = request.form.get('gender',           '').strip()
        province   = request.form.get('province',         '').strip()
        city       = request.form.get('city',             '').strip()
        barangay   = request.form.get('barangay',         '').strip()
        password   = request.form.get('password',         '').strip()
        confirm_pw = request.form.get('confirm_password', '').strip()
        form = dict(identifier=identifier, full_name=full_name, age=age,
                    gender=gender, province=province, city=city, barangay=barangay)

        if not identifier:                              errors['identifier']       = 'Required.'
        elif not is_valid_email(identifier) and not is_valid_phone(identifier):
                                                        errors['identifier']       = 'Enter valid email or 09XXXXXXXXX.'
        if not full_name:                               errors['full_name']        = 'Required.'
        elif len(full_name) < 2:                        errors['full_name']        = 'Min 2 chars.'
        if not age:                                     errors['age']              = 'Required.'
        elif not age.isdigit() or not (1 <= int(age) <= 120):
                                                        errors['age']              = 'Enter valid age (1-120).'
        if not gender:                                  errors['gender']           = 'Select gender.'
        if not province:                                errors['province']         = 'Select province.'
        if not city:                                    errors['city']             = 'Select city.'
        if not barangay:                                errors['barangay']         = 'Select barangay.'
        if not password:                                errors['password']         = 'Required.'
        elif len(password) < 6:                         errors['password']         = 'Min 6 chars.'
        elif not re.search(r'[A-Za-z]', password) or not re.search(r'\d', password):
                                                        errors['password']         = 'Must contain letters and numbers.'
        if not confirm_pw:                              errors['confirm_password'] = 'Confirm your password.'
        elif password != confirm_pw:                    errors['confirm_password'] = 'Passwords do not match.'

        if not errors:
            try:
                db  = get_db()
                cur = db.cursor(dictionary=True)
                email  = identifier if is_valid_email(identifier) else None
                mobile = identifier if is_valid_phone(identifier) else None
                cur.execute('SELECT id FROM users WHERE email=%s OR mobile=%s', (email, mobile))
                if cur.fetchone():
                    errors['identifier'] = 'Already registered. Please log in.'
                else:
                    hashed  = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
                    address = f"{barangay}, {city}, {province}"
                    cur.execute("""
                        INSERT INTO users (email, mobile, full_name, age, gender, address, password)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """, (email, mobile, full_name, int(age), gender, address, hashed))
                    db.commit(); cur.close(); db.close()
                    flash(f'Welcome, {full_name}! Your account is ready.', 'success')
                    return redirect(url_for('login'))
                cur.close(); db.close()
            except Exception as e:
                errors['general'] = f'Database error: {e}'

    return render_template('register.html', errors=errors, form=form)


# ── LOGOUT ──────────────────────────────────────────────────────
@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('landing'))


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)