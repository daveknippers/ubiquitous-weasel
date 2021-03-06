
import time, argparse, os, sys, queue
import pandas as pd
import irc.bot


from oauth_token import oauth_token
from memory import Spelunker

from sqlalchemy import create_engine
from dbsecrets import USER, PASSWORD, HOST, PORT, DATABASE
from threading import Lock, Timer, Thread

process_q = queue.Queue()
priv_msg_q = queue.Queue()
pub_msg_q = queue.Queue()

class Bookie(Thread):

	def __init__(self,streamer_name):
		Thread.__init__(self)

		self.streamer_name = streamer_name

		if not os.path.exists('death_types.csv'):
			raise ValueError('death_types.csv not found, cannot continue.')

		self.death_types_df = pd.read_csv('death_types.csv',index_col=False,
				names=['death_id','internal_death_name','death_name','death_reason','multiplier'],
				dtype={'death_id':int,'internal_death_name':str,'death_name':str, 'death_reason':str, 'multiplier':int})

		self.death_reason_map = {}
		for (k,v) in self.death_types_df[['death_id','death_reason']].values:
			self.death_reason_map[k] = v

		self.death_map = {}
		for (k,v) in self.death_types_df[['death_id','death_name']].values:
			self.death_map[k] = v

		self.death_multiplier_map = {}
		for (k,v) in self.death_types_df[['death_id','multiplier']].values:
			self.death_multiplier_map[k] = v

	def run(self):
		#connect to database and pull down bet_ledger
		pg_db = create_engine(f'postgresql://{DBUSER}:{PASSWORD}@{HOST}:{PORT}/{DATABASE}',echo=False)
		connection = pg_db.connect()
		bet_ledger_df = pd.read_sql('SELECT * FROM Kali.bet_ledger',pg_db)

		active_bets = {}

		while True:
			event = process_q.get(block=True)

			# return user's balance
			if isinstance(event,str):
				user = event
				try:
					balance = bet_ledger_df[bet_ledger_df['nickname'] == user]['golden_daves'].values[0]
				except: # maybe figure out actual exceptions that can trigger here so we can not do bad practice
					balance = 1000
					bet_ledger_df = bet_ledger_df.append([{'nickname':user,'golden_daves':balance}],ignore_index=True)
					# single row in a table change?
					# better WIPE THE ENTIRE TABLE OUT and REWRITE THE ENTIRE THING
					bet_ledger_df.to_sql('Kali.bet_ledger',pg_db,index=False,if_exists='replace')

				if user in active_bets.keys():
					bets = active_bets[user]
					total_bets = sum(map(lambda x: x[0],bets))
				else:
					total_bets = 0
				new_balance = balance - total_bets
				priv_msg_q.put((user,'You have {} GOLDEN DAVES.'.format(new_balance)))

			# player has won/died, issue payouts
			elif len(event) == 2:
				death_cause_id, died_on_level = event

				won_game = False
				unknown_error = False

				if death_cause_id is None:
					death_cause = None
					won_game = True
					pub_msg_q.put("Streamer has won! Your GOLDEN DAVES are now forfeit.")
				else:
					try:
						death_cause = self.death_map[death_cause_id]
						multiplier = self.death_multiplier_map[death_cause_id]
					except KeyError:
						pub_msg_q.put("OH LORDY, I HOPE THERE'S TAPES")
						pub_msg_q.put("Streamer was killed by {}, but I don't have a record of that cause of death.")
						pub_msg_q.put("Not to worry, your GOLDEN DAVES are still safe. If you think you deserve something, please write an essay detailing why and I promise I'll take it into consideration.")
						active_bets = {}
						death_cause = str(death_cause_id)
						multiplier = 0
						unknown_error = True

				#Store death cause in DB
				sql = f"INSERT INTO Kali.death_tracker(cause_of_death,player,level) values('{death_cause}','{self.streamer_name}','{died_on_level}')"
				connection.execute(sql)

				payouts = {}
				n_bets = 0
				n_winning_bets = 0
				n_total_winnings = 0

				for u,acls in active_bets.items():
					try:
						user_payouts = payouts[u]
					except KeyError:
						user_payouts = 0

					for a,c,l in acls:
						if death_cause == c:
							user_payouts += a*(multiplier-1)
							n_winning_bets += 1
							n_bets += 1
							n_total_winnings += a*(multiplier-1)
						else:
							user_payouts -= a
							n_bets += 1
					payouts[u] = user_payouts

				# this is probably slow but i'm not worried, scalability is a problem we'd like to have
				def adjust_balance(row):
					try:
						payout_amount = payouts[row['nickname']]
					except KeyError:
						payout_amount = 0
					return payout_amount+row['golden_daves']

				bet_ledger_df['golden_daves'] = bet_ledger_df.apply(adjust_balance,axis=1)

				def bump_cash(cash):
					if cash < 100: return 100
					else: return cash

				# single row in a table change?
				# better WIPE THE ENTIRE TABLE OUT and REWRITE THE ENTIRE THING
				bet_ledger_df['golden_daves'] = bet_ledger_df['golden_daves'].apply(bump_cash)
				bet_ledger_df.to_sql('Kali.bet_ledger',pg_db,index=False,if_exists='replace')

				for u,p in payouts.items():
					balance = bet_ledger_df[bet_ledger_df['nickname'] == u]['golden_daves'].values[0]
					if p == 0:
						msg = 'Your winning bets made up for your losing bets. Your balance is {} GOLDEN DAVES.'.format(balance)
					if p > 0:
						msg = 'You won {} GOLDEN DAVES. Your new balance is {}.'.format(p,balance)
					if p < 0: 
						msg = 'You lost {} GOLDEN DAVES. Your new balance is {}.'.format(p*-1,balance)

					priv_msg_q.put((u,msg))


				if not won_game and not unknown_error:
					explanation = self.death_reason_map[death_cause_id]

					compose_message = 'Streamer has found the sweet release of death.'
					if n_bets > 0 and n_winning_bets > 0:
						compose_message += ' They were {} There were {} bet(s) total and {} winning bet(s). A total of {} GOLDEN DAVES were won.'.format(explanation,n_bets,n_winning_bets,n_total_winnings)
					elif n_bets > 0:
						compose_message += ' They were {} There were {} bet(s) total, all losers.'.format(explanation,n_bets)
					else:
						compose_message += ' They were {}'.format(explanation)
					pub_msg_q.put(compose_message)

				active_bets = {}

			# accept bet
			elif len(event) == 4:
				user,cause,amount,level = event
				try:
					balance = bet_ledger_df[bet_ledger_df['nickname'] == user]['golden_daves'].values[0]
				except:
					balance = 1000
					# single row in a table change?
					# better WIPE THE ENTIRE TABLE OUT and REWRITE THE ENTIRE THING
					bet_ledger_df = bet_ledger_df.append([{'nickname':user,'golden_daves':balance}],ignore_index=True)
					bet_ledger_df.to_sql('Kali.bet_ledger',pg_db,index=False,if_exists='replace')

				if user in active_bets.keys():
					bets = active_bets[user]
					total_bets = sum(map(lambda x: x[0],bets))
				else:
					total_bets = 0

				if total_bets+amount > balance:
					priv_msg_q.put((user,'Insufficent balance. You have {} GOLDEN DAVES available.'.format(balance-total_bets)))
				elif cause not in self.death_map.values():
					priv_msg_q.put((user,'Invalid cause of death.'))
				else:
					try:
						active_bets[user]
					except KeyError:
						active_bets[user] = []
					#Store bet into database
					active_bets[user].append((amount,cause,level))
					sql = f"INSERT INTO Kali.bets_tracker(bet_on_user,level,death,betting_user) values('{self.streamer_name}','{level}','{cause}','{user}')"




class KaliBot(irc.bot.SingleServerIRCBot):

	def __init__(self, server, channel, streamer_name, nickname):
		irc.bot.SingleServerIRCBot.__init__(self, [server], nickname, nickname)

		self.channel = channel
		self.sp = Spelunker()

		self.enable_bets = True
		
		self.timer = self.sp.game_timer
		self.level = self.sp.level
		self.is_dead = self.sp.is_dead
		self.has_ankh  = self.sp.has_ankh
		self.killed_by = self.sp.killed_by
		self.triggered_shoppie = False
		
		self.lock = Lock()

		self.bookie = Bookie(streamer_name)
		self.bookie.start()

		self.reactor.scheduler.execute_every(2,self.process_private)
		self.reactor.scheduler.execute_every(2,self.process_public)
		self.reactor.scheduler.execute_every(2,self.process_spelunker)


	def process_spelunker(self):
		self.lock.acquire()

		sp = self.sp
		current_timer = sp.game_timer
		current_level = sp.level
		current_is_dead = sp.is_dead
		current_has_ankh = sp.has_ankh
		current_angry_shopkeeper = sp.angry_shopkeeper
		current_killed_by = sp.killed_by

		if self.has_ankh and not current_has_ankh:
			pub_msg_q.put('Good thing I have the Ankh.')
		if not self.triggered_shoppie and current_angry_shopkeeper and not current_is_dead:
			pub_msg_q.put('Always bet on the shopkeeper.')
			self.triggered_shoppie = True

		if not self.is_dead and current_is_dead:
			process_q.put((current_killed_by,current_level))
			self.triggered_shoppie = False
			self.enable_bets = False
			Timer(15,self.start_betting).start()

		elif not current_is_dead:
			if self.timer == current_timer:
				if current_level == 0 and (self.level == 16 or self.level == 20):
					self.enable_bets = False
					self.triggered_shoppie = False
					Timer(15,self.start_betting).start()
					process_q.put((None,self.level))
		
		self.timer = current_timer
		self.is_dead = current_is_dead
		self.level = current_level
		self.has_ankh = current_has_ankh
		self.killed_by = current_killed_by
		
		self.lock.release()

	def process_private(self):
		try:
			usr,msg = priv_msg_q.get(block=False)
			self.send_message(usr,msg)
			print('private message: {} {}'.format(usr,msg))
		except queue.Empty:
			pass

	def process_public(self):
		try:
			msg = pub_msg_q.get(block=False)
			self.send_pub_message(msg)
			print('public message: {}'.format(msg))
		except queue.Empty:
			pass
		
	def start_betting(self):
		self.lock.acquire()

		pub_msg_q.put('Now accepting new bets.')
		self.enable_bets = True

		self.lock.release()

	def send_message(self, user, message):
		self.connection.privmsg(self.channel,'/w {} {}'.format(user,message))
		
	def send_pub_message(self, message):
		self.connection.privmsg(self.channel, message)
			
	def on_pubmsg(self, c, e):
		if len(e.arguments) == 1:
			if e.arguments[0][0] == '!':
				msg = e.arguments[0].split("!", 1)[1:][0]
			else: msg = None
		else:
			msg = None

		if msg is None:
			return

		if len(msg) > 0:
			self.do_command(msg,e)

	def on_whisper(self, c, e):
		self.on_pubmsg(c,e)

	def on_welcome(self, c, e):
		print('joining {}'.format(self.channel))
		c.send_raw("CAP REQ :twitch.tv/commands")
		c.join(self.channel)
		c.set_rate_limit(100/30)

	def do_command(self,msg,meta):
		user = meta.source.split("!")[0]
		msg = msg.strip().split()
		if len(msg) == 0:
			return

		command = msg[0]

		if command == 'help':
			priv_msg_q.put((user,'Available commands: !bet death_cause amount, !balance'))

		if command == 'bet':
			if self.enable_bets:
				if len(msg) != 3:
					reason = "Invalid betting parameters. Try !bet death_cause amount. The causes can be found on bunny_funeral's channel description below."
					priv_msg_q.put((user,reason))
				else:
					amount = msg[2]
					try:
						amount = int(amount)
						if amount < 1:
							raise ValueError()
					except ValueError:
						reason = 'Invalid betting amount. Format is !bet death_cause amount'
						priv_msg_q.put((user,reason))
						amount = None

					if amount is not None:
						cause = msg[1]
						process_q.put((user,cause,amount,self.level))
			else:
				priv_msg_q.put((user,'Betting is currently disallowed. Betting will be reenabled in less than fifteen seconds.'))
		
		if command == 'balance':
			print('printing balance for {}'.format(user))
			process_q.put((user))


	def get_balance(self,user):
		pass

def KaliRoulette(streamer_name, bot_name):

	server = irc.bot.ServerSpec('irc.twitch.tv', port=6667, password=oauth_token)
	bot = KaliBot(server, '#{}'.format(streamer_name), streamer_name, bot_name)
	bot.start()

if __name__ == "__main__":
	parser = argparse.ArgumentParser(description='Start the Kali Roulette memory reader/IRC bot')
	parser.add_argument('-s', '--streamer', dest='streamer', type=str, help='your twitch username')
	parser.add_argument('-b', '--bot-name', dest='bot_name', type=str, help="your bot's twitch username")
	args = parser.parse_args()
	if args.streamer is None or args.bot_name is None:
		parser.print_help()
	else:
		KaliRoulette(args.streamer,args.bot_name)
				   
