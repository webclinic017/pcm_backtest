import logging, numpy as np, time, pandas as pd

from abc import abstractmethod
from kombu import binding
from tqdm import tqdm
from functools import lru_cache
from threading import Thread
from math import ceil

from .pos import Position
from .base import BaseConsumer
from .event import SignalEventPct, OrderEvent
from .conf import LONG, SHORT, EXIT, MKT, BUY, SELL, LOCAL_TZ
from .util import clean_timestamp
from .errors import OverFilling

logger = logging.getLogger('Strategy')



class BaseStrategy(BaseConsumer):
	"""Strategy is an abstract base class providing an interface for
	all subsequent (inherited) strategy handling objects.

	Goal
	----
	The goal of a (derived) Strategy object 
	- based on the inbound 'Tick', calcualte signals
	- 'Signal' is at the symbol level which will be published

	Note
	----
	This is designed to work both with historic and live data as
	the Strategy object is agnostic to the data source,
	since it obtains the 'Tick' object from MarketEvent message
	"""
	def __init__(
		self, symbol_list, allocation, freq, positions, warmup=0,
		start=None, end=None, fixed_allocation=True,
		batch_size=10000
	):
		"""
		Parameter:
		----------
		symbol_list (list): A list of Contract perm_tick (for data)
		allocation (float): Dollar amount that this strategy is able to use
		freq (conf.FREQ): Data Frequency type for this strategy (for data)
		positions (dict of dict):
			A dictionary with perm_tick and a dictionary of arguments

			- pct_portfolio (float): percentage of the allocation
			- rebalance (int): # of days to rebalance to pct_portfolio
			- hard_stop (float): hard drawdown gate to close position
		warmup (int): # of days to warmup the strategy
		env_type (string): {'BACKTEST', 'PAPPER', 'LIVE'}
			which environment to run the startegy
		start, end (datetime):
			Only for backtesting to specificy the range of data to test
		"""
		n = ceil(freq.one_day)
		num_pos = len(positions)

		# getting neccesary parameters
		self.symbol_list = symbol_list
		self.freq = freq
		self.warmup = warmup * n

		if start:
			self.start_dt = pd.Timestamp(
				clean_timestamp(pd.Timestamp(start, tz=LOCAL_TZ)).date()
			)

		if end:
			self.end_dt = pd.Timestamp(
				clean_timestamp(pd.Timestamp(end, tz=LOCAL_TZ)).date()
			) + pd.DateOffset(seconds=-1, days=1)


		# allocation parameters for tracking portfolio
		self.allocation = allocation
		self.cash = allocation
		self.commission = 0
		self.fixed_allocation = fixed_allocation

		pos_dict = {}
		for perm_tick, v in positions.items():
			# want to have position, must know its market ticks for decision
			if perm_tick not in self.symbol_list:
				self.symbol_list.append(perm_tick)

			pos = Position(
				perm_tick,
				pct_portfolio=v.get('pct_portfolio', 1/num_pos),
				rebalance=v.get('rebalance', 0) * n,
				hard_stop=v.get('hard_stop', 0),
			)
			pos_dict[perm_tick] = pos
		self.pos = pos_dict

		# starting is always 0, it will increment itself every market tick
		self.t = 0
		self._hist = []
		self.batch_size = batch_size

		super().__init__(comp_type='STGY', required=['feed', 'exe'])


	@abstractmethod
	def calculate_signals(self):
		"""Provide the mechanism to calculate a list of signals"""
		raise NotImplementedError(
			"Should implement calculate_signals()\n" + \
			"By calling this method to calculate 'Signal' Events"
		)
		
	def update_data(self, ticks):
		pass

	def on_hard_stop(self, symbol):
		pass

	def on_rebalance(self, symbol):
		pass

	def has_position(self, symbol):
		return self.pos[symbol].has_position

	def has_open_orders(self, symbol):
		return self.pos[symbol].has_open_orders

	def has_long(self, symbol):
		return self.pos[symbol].has_long

	def has_short(self, symbol):
		return self.pos[symbol].has_short

	@property
	def nav(self):
		"""Net Account Value / Net Liquidating Value"""
		return sum(pos.mv for pos in self.pos.values()) + self.cash

	@property
	@lru_cache(maxsize=32)
	def warmup_key(self):
		return 'warmup.{}'.format(self.name)

	@property
	@lru_cache(maxsize=32)
	def next_key(self):
		return 'next.{}'.format(self.name)

	@property
	@lru_cache(maxsize=32)
	def order_key(self):
		return 'order.{}'.format(self.name)

	@property
	def bp(self):
		if self.fixed_allocation:
			return self.allocation
		else:
			return self.nav


	def start(self):
		while self.status != 'RUNNING':	
			time.sleep(2)

		# setting up progress bar
		self._pbar = tqdm(
			total=int(np.ceil(
				pd.bdate_range(self.start_dt, self.end_dt).size
				* np.ceil(self.freq.one_day)
			)),
			miniters=int(np.ceil(self.freq.one_day)),
			unit=' tick<{}>'.format(self.freq.value),
		)

		# publish event to get started
		self.basic_publish('warmup', sender=self.id)
		self.basic_publish('next', sender=self.id)


	def subscriptions(self):
		return [
			('ack-reg-feed', self.id, self.on_ack_reg_feed),
			('ack-dereg_feed', self.id, self.on_ack_dereg_feed),
			('ack-reg-exe', self.id, self.on_ack_reg_exe),
			('ack-dereg-exe', self.id, self.on_ack_dereg_exe),
			('eod', self.id, self.on_eod),
			('tick', self.id, self.on_market),
			('fill', self.id, self.on_fill),
		]


	def on_msg(self, body, message):
		body = decompress_data(body)
		key = message.delivery_info['routing_key'].split('.')
		event = key[0]
		
		if event == 'tick':
			ticks = event_from_dict(body)
			self.on_market(ticks)

		elif event == 'fill':
			self.on_fill(event_from_dict(body))

		elif event == 'eod':
			self.on_eod()

		elif event == 'ack-reg-feed':
			self.on_ack_reg_feed()

		elif event == 'ack-dereg-feed':
			self.on_ack_dereg_feed()

		elif event == 'ack-reg-exe':
			self.on_ack_reg_exe()

		elif event == 'ack-dereg-exe':
			self.on_ack_dereg_exe()

		# message.ack()


	def on_ack_reg_feed(self, oid, body):
		self.required['feed'] = True

	def on_ack_reg_exe(self, oid, body):
		self.required['exe'] = True

	def on_ack_dereg_feed(self, oid, body):
		self.required['feed'] = False

	def on_ack_dereg_exe(self, oid, body):
		self.required['exe'] = False


	def on_eod(self, oid, body):
		"""Handlering End of Data Event"""
		self._pbar.update(self._pbar.total - self._pbar.n)
		if self._hist:
			self._process_position_entries()
		self._pbar.close()

		self.basic_publish('dereg-feed', sender=self.id)
		self.basic_publish('dereg-exe', sender=self.id)

		self._stop()


	def on_fill(self, oid, body):
		"""Upon filled order
		- update strategy's position, spot position reversion
		- update holding time
		- update position quantity

		Parameter:
		----------
		fill (Fill Event)
		"""
		fill = body['fill']

		# update the position first
		self.pos[fill.symbol].on_fill(fill)

		# getting data from the fill event
		Q = fill.quantity
		K, D, C = fill.fill_cost, fill.fill_type, fill.commission

		cost = D.value * K * Q

		self.commission += C
		self.cash -= cost + C


	def on_market(self, oid, body):
		"""On market event
		- update information for each existing poistion
		- generate orders for rebalancing()
		- the strategy will calculate signal(s)
		- and publish them to the exchange for processing
		- then a "done" will be published to indicate
			the strategy is finish doing everything this heartbeat
		- so then the risk manager will collect all signals
			before sending order for execution

		Parameter:
		----------
		ticks (Market Event)
		"""
		ticks = body['ticks']
		self._update_data(ticks)

		if self.t >= self.warmup:
			self._calculate_signals()

			# publish generated signals
			bp = self.bp  # current snap_shot of buying power
			for S, pos in self.pos.items():
				for order, lvl in pos.generate_orders(self.bp):
					pos.confirm_order(order)
					self.basic_publish('order', sender=self.id, order=order)

					used_bp = self.on_order(order, lvl, bp)
					bp -= used_bp
				
			# save old strategy performance history
			self._pbar.update(1)
		
		if ticks.timestamp >= self.start_dt:
			self.basic_publish('next', sender=self.id)

		if self.t >= self.warmup:
			self._save_positions()


	def on_order(self, order, lvl, bp):
		"""Handling new order
		- Orders are generated from signals
		- will have to check currently avaliable buying power before publish

		Parameter:
		---------
		order (Order Event)
		lvl (str): Level of urgency for the order
			This flag will be used to call corresponding callback
		bp (float): The amount of avaliable buying power

		Return:
		-------
		used buying power (float)
		"""
		S = order.symbol

		need_bp = order.quantity * self.ticks[S].close
		if need_bp <= bp:
			used_bp = need_bp

			if lvl == 'hard_stop':
				self.on_hard_stop(S)
			elif lvl == 'rebalance':
				self.on_rebalance(S)
		else:
			used_bp = 0
		return used_bp


	def generate_signal(self, symbol, signal_type, **kws):
		"""Generate a signal that will stored at Strategy level
		- Then all signals will be batch processed

		Parameter
		---------
		symbol: str, the target symbol for the signal
		signal_type: {LONG, SHORT, EXIT}
		kws: additional arguments passes to the SignalEvent class
			- especially the `strength` for percentage of portfolio
			- if not passed, the default `pct_portfolio` will be used
		"""
		self.pos[symbol]._generate_signal(signal_type, lvl='normal', **kws)


	def _calculate_signals(self):
		# update existing position information
		for pos in self.pos.values():
			pos._calculate_signals()

		self.calculate_signals()


	def _update_data(self, ticks):
		"""Update the existing state of strategies
		- based on given market observation

		Note:
		-----
		1. It will always be called before calculating the new signal
		2. this will be called no matter strategy is in warmup period or not
			becuase warmup period is used for gathering nessceary data
		"""
		self.ticks = ticks
		self.t += 1

		for S, pos in self.pos.items():
			pos._update_data(ticks[S])

		self.update_data(ticks)


	def _save_positions(self):
		output = {
			'timestamp': self.ticks.timestamp, 't': self.t,
			'cash': self.cash, 'commission': self.commission,
			'nav': self.nav,
		}
		self._hist.append(output)
		
		# if self.t % self.batch_size == 0:
			# self._process_position_entries()


	# def _process_position_entries(self):
	# 	data = compress_data(self._hist)
	# 	process_new_position.delay(self.name, positions=data)
	# 	self._hist.clear()