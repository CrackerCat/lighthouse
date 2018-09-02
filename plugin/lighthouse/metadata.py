import time
import Queue
import bisect
import logging
import weakref
import threading
import collections

from lighthouse.util.misc import *
from lighthouse.util.disassembler import disassembler

logger = logging.getLogger("Lighthouse.Metadata")

#------------------------------------------------------------------------------
# Metadata
#------------------------------------------------------------------------------
#
#    To aid in performance, the director lifts and indexes a limited
#    representation of the database (referred to as 'metadata' in code.)
#
#    This lifted metadata effectively eliminates what becomes otherwise
#    costly runtime communication between the director and IDA. It is also
#    tailored for efficiency and speed to complement our needs. Once built,
#    the metadata cache stands completely independent of IDA.
#
#    This opens up a realm of interesting possibilities. With this model,
#    we can easily move any heavy director based compute to asynchrnous
#    python-only threads without disrupting the user, or IDA. (v0.4.0)
#
#    However, there are two main caveats of this model -
#
#    1. The cached 'metadata' representation may not always be true to
#       state of the database. For example, if the user defines/undefines
#       functions, the metadata cache will not be aware of such changes.
#
#       Lighthouse will try to update the director's metadata cache when
#       applicable, but there are instances when it will be in the best
#       interest of the user to manually trigger a refresh of the metadata.
#
#    2. Building the metadata comes with an upfront cost, but this cost has
#       been reduced as much as possible. For example, generating metadata
#       for a database with ~17k functions, ~95k nodes (basic blocks), and
#       ~563k instructions takes only ~2.5 seconds.
#
#       This will be negligible for small-medium sized databases, but may
#       still be jarring for larger databases.
#
#    Ultimately, this model provides us responsive user experience at the
#    expense of the ocassional inaccuracies that can be corrected by a
#    reasonably low cost refresh.
#

#------------------------------------------------------------------------------
# Database Level Metadata
#------------------------------------------------------------------------------

class DatabaseMetadata(object):
    """
    Fast access database level metadata cache.
    """

    def __init__(self):

        # name & imagebase of the executable this metadata is based on
        self.filename = ""
        self.imagebase = -1

        # database defined instructions
        self.instructions = []

        # database defined nodes (basic blocks)
        self.nodes = {}

        # database defined functions
        self.functions = {}
        self._blank_function = FunctionMetadata(-1)

        # database metadata cache status
        self.cached = False

        # lookup list members
        self._stale_lookup = False
        self._name2func = {}
        self._last_node = []           # TODO/HACK: blank iterable for now
        self._node_addresses = []
        self._function_addresses = []

        # placeholder attribute for disassembler event hooks
        self._rename_hooks = None

        # metadata callbacks (see director for more info)
        self._function_renamed_callbacks = []

        # asynchrnous metadata collection thread
        self._refresh_worker = None
        self._stop_threads = False

    def terminate(self):
        """
        Cleanup & terminate the metadata object.
        """
        self.abort_refresh(join=True)
        if self._rename_hooks:
            self._rename_hooks.unhook()

    #--------------------------------------------------------------------------
    # Providers
    #--------------------------------------------------------------------------

    def get_instructions_slice(self, start_address, end_address):
        """
        Get the instructions in the given range of addresses.
        """
        index_start = bisect.bisect_left(self.instructions, start_address)
        index_end   = bisect.bisect_left(self.instructions, end_address)
        return self.instructions[index_start:index_end]

    def get_node(self, address):
        """
        Get the node (basic block) metadata for a given address.

        This function provides fast lookup of node metadata for an
        arbitrary address (ea). Assuming the address falls within a
        defined function, there should exist a graph node for it.

        We use bisection across the defined node (basic block) addresses
        to perform a fuzzy lookup of the closest node just prior to the
        target address.

        If the target address falls within the probed node, the node's
        metadata is returned. Failure returns None.
        """

        #
        # TODO/NOTE/PERF:
        #
        #   sortedcontainers.SortedDict would be ideal type for the nodes and
        #   functions dictionaries. It means we wouldn't have to maintain an
        #   entirely seperate list of addresses for quick bisection.
        #
        #   but I don't want to hassle people with a dependency on an external
        #   package for lighthouse. so we'll keep it in-house and old school.
        #

        #found = self.nodes.iloc[(self.nodes.bisect_left(address) - 1)]

        # fast path, effectively a LRU cache of 1 ;P
        if address in self._last_node:
            return self._last_node

        #
        # perform an on-demand / inline refresh of the lookup lists to ensure
        # that our bisections will be correct.
        #
        # NOTE:
        #
        #  Internally, the refresh is only performed if the lists are stale.
        #
        #  This means that 99.9% of the time, this call will add virtually
        #  no overhead to the 'get_node' call.
        #
        #  TODO/PERF: double check that this is actually performant, because
        #  I am not so sure the overhead is non-zero anymore...
        #

        self._refresh_lookup()

        #
        # use the lookup lists to do a 'fuzzy' lookup, locating the index of
        # the closest known (cached) node address (rounding down)
        #

        node_index = bisect.bisect_right(self._node_addresses, address) - 1

        #
        # if the identified node contains our target address, it is a match
        #

        node = self.nodes.get(self._node_addresses[node_index], None)
        if node and address in node:
            self._last_node = node
            return node

        # node not found...
        return None

    def get_function(self, address):
        """
        Get the function metadata for a given address.

        See get_node() for more information.

        If the target address falls within a function, the function's
        metadata is returned. Failure returns None.
        """

        # locate the node the given address falls within
        node_metadata = self.get_node(address)
        if not node_metadata:
            return None

        # return the function metadata corresponding to this node.
        return node_metadata.function

    def get_closest_function(self, address):
        """
        Get the function metadata for the function closest to the give address.
        """

        # sanity check
        if not self._function_addresses:
            return None

        # get the closest insertion point of the given address
        pos = bisect.bisect_left(self._function_addresses, address)

        # the given address is a min, return the first known function
        if pos == 0:
            return self.functions[self._function_addresses[0]]

        # given address is a max, return the last known function
        if pos == len(self._function_addresses):
            return self.functions[self._function_addresses[-1]]

        # select the two candidate addresses
        before = self._function_addresses[pos - 1]
        after  = self._function_addresses[pos]

        # return the function closest to the given address
        if after - address < address - before:
            return self.functions[after]
        else:
            return self.functions[before]

    def get_function_num(self, address):
        """
        Get the function number for a given address.
        """
        return self._function_addresses.index(address)

    def get_function_by_name(self, function_name):
        """
        Get the function metadata for a given function name.
        """
        try:
            return self.functions[self._name2func[function_name]]
        except (IndexError, KeyError):
            pass
        return None

    def get_function_by_num(self, function_num):
        """
        Get the function metadata for a given function number.
        """
        return self.functions[self._function_addresses[function_num]]

    def flatten_blocks(self, basic_blocks):
        """
        Flatten a list of basic blocks (address, size) to instruction addresses.

        This function provides a way to convert a list of (address, size) basic
        block entries into a list of individual instruction (or byte) addresses
        based on the current metadata.

        If no corresponding metadata instruction can be found for a given
        address while walking the basic block ranges, the current address being
        flattened is saved as a 'byte address' to the output list.

        A byte address is basically an address that points to one 'undefined
        byte', such as a byte in an 'undefined instruction'
        """
        output = []

        # sanity check
        if not basic_blocks:
            return output

        # loop through every given basic block (input)
        for address, size in basic_blocks:
            instructions = self.get_instructions_slice(address, address+size)
            output.extend(instructions)

        # return the list of addresses
        return output

    def is_big(self):
        """
        Return an size classification of the database / metadata.
        """
        return len(self.functions) > 50000

    #--------------------------------------------------------------------------
    # Refresh
    #--------------------------------------------------------------------------

    def refresh(self, function_addresses=None, progress_callback=None):
        """
        Refresh the entire database metadata (asynchronously)
        """
        assert self._refresh_worker == None, 'Refresh already running'
        result_queue = Queue.Queue()

        #
        # reset the async abort/stop flag that can be used used to cancel the
        # ongoing refresh task
        #

        self._stop_threads = False

        #
        # kick off an asynchronous metadata collection task
        #

        self._refresh_worker = threading.Thread(
            target=self._async_refresh,
            args=(result_queue, function_addresses, progress_callback,)
        )
        self._refresh_worker.start()

        #
        # immediately return a queue to the user that will shepard the future
        # result of the metadata refresh from the thread upon completion
        #

        return result_queue

    def abort_refresh(self, join=False):
        """
        Abort a running refresh.

        To guarantee the refresh has been aborted, the caller can wait for
        result_queue (as recieved from the call to self.refresh()) to
        return an item.

        A 'None' item returned from the refresh() future (result_queue)
        indicates an aborted refresh. In theory, the state of metadata
        should be partially refreshed and still usable.
        """

        #
        # the refresh worker (if it exists) can be ripped away at any time.
        # take a local reference to avoid a double fetch problems
        #

        worker = self._refresh_worker

        #
        # if there is no worker present or running (cleaning up?) there is
        # nothing for us to abort. Simply reset the abort flag (just in case)
        # and return immediately
        #

        if not (worker and worker.is_alive()):
            self._stop_threads = False
            self._refresh_worker = None
            return

        # signal the worker thread to stop
        self._stop_threads = True

        # if requested, don't return until the worker thread has stopped...
        if join:
            worker.join()

    def _refresh_instructions(self):
        """
        Refresh the instructions list from function metadata.
        """
        instructions = []
        for function_metadata in self.functions.itervalues():
            instructions.extend(function_metadata.instructions)
        instructions = list(set(instructions))
        instructions.sort()

        # commit the updated intruction list
        self.instructions = instructions

    def _refresh_lookup(self):
        """
        Refresh the fast lookup address lists from function metadata.

        This will only refresh the lists if they are believed to be stale.
        """

        #
        # fast lookup lists are simply sorted address lists of functions, nodes
        # or possibly other (future) metadata.
        #
        # we create sorted lists of just these metadata addresses so that we
        # can use them for fast, fuzzy address lookup (eg, bisect) later on.
        #
        #  c.f:
        #   - get_node(ea)
        #   - get_function(ea)
        #

        # if the lookup lists are fresh, there's nothing to do
        if not self._stale_lookup:
            return False

        # update the lookup lists
        self._last_node = []
        self._name2func = { f.name: f.address for f in self.functions.itervalues() }
        self._node_addresses = sorted(self.nodes.keys())
        self._function_addresses = sorted(self.functions.keys())

        # lookup lists are no longer stale, reset the stale flag as such
        self._stale_lookup = False

        # refresh success
        return True

    #--------------------------------------------------------------------------
    # Metadata Collection
    #--------------------------------------------------------------------------

    @not_mainthread
    def _async_refresh(self, result_queue, function_addresses, progress_callback):
        """
        TODO/COMMENT
        """

        # pause our rename listening hooks (more performant collection)
        if self._rename_hooks:
            self._rename_hooks.unhook()

        #
        # if the caller provided no function addresses to target for refresh,
        # we will perform a complete metadata refresh of all database defined
        # functions. let's retrieve that list from the database now...
        #

        if not function_addresses:
            function_addresses = disassembler.execute_read(
                disassembler.get_function_addresses
            )()

        # refresh main meteadata properties
        self._async_refresh_properties()

        # refresh metadata asynchronously
        completed = self._async_collect_metadata(
            function_addresses,
            progress_callback
        )

        # regenerate the instruction list from collected metadata
        self._refresh_instructions()

        # refresh the function/node lookup lists
        self._refresh_lookup()

        #
        # NOTE:
        #
        #   creating the hooks inline like this is less than ideal, but they
        #   they have been moved here (from the metadata constructor) to
        #   accomodate shortfalls of the Binary Ninja API.
        #
        # TODO/FUTURE/V35:
        #
        #   it would be nice to move these back to the constructor once the
        #   Binary Ninja API allows us to detect BV / sessions as they are
        #   created, and able to load plugins on such events.
        #

        #----------------------------------------------------------------------

        # create the disassembler hooks to listen for rename events
        if not self._rename_hooks:
            self._rename_hooks = disassembler.create_rename_hooks()
            self._rename_hooks.renamed = self._name_changed
            self._rename_hooks.metadata = weakref.proxy(self)

        #----------------------------------------------------------------------

        # resume our rename listening hooks
        self._rename_hooks.hook()

        # send the refresh result (good/bad) incase anyone is still listening
        if completed:
            self.cached = True
            result_queue.put(True)
        else:
            result_queue.put(False)

        # clean up our thread's reference as it is basically done/dead
        self._refresh_worker = None

        # thread exit...
        return

    @disassembler.execute_read
    def _async_refresh_properties(self):
        """
        TODO/COMMENT
        """
        self.filename = disassembler.get_root_filename()
        self.imagebase = disassembler.get_imagebase()

    @not_mainthread
    def _async_collect_metadata(self, function_addresses, progress_callback):
        """
        Asynchronously collect metadata from the underlying database.
        """
        CHUNK_SIZE = 150
        completed = 0

        start = time.time()
        #----------------------------------------------------------------------

        # loop through every defined function (address) in the database
        for addresses_chunk in chunks(function_addresses, CHUNK_SIZE):

            # synchronize and read (collect) function metadata from the
            # database in controlled chunks (faster in chunks than one by one)
            fresh_metadata = collect_function_metadata(addresses_chunk)

            # update the database metadata with the collected metadata
            self._update_functions(fresh_metadata)

            # report progress to an external subscriber
            if progress_callback:
                completed += len(addresses_chunk)
                progress_callback(completed, len(function_addresses))

            # if an abort was requested, bail immediately
            if self._stop_threads:
                return False

            # sleep some so we don't choke the mainthread
            time.sleep(.0015)

        #----------------------------------------------------------------------
        end = time.time()
        logger.debug("Metadata collection took %s seconds" % (end - start))

        # completed normally
        return True

    def _update_functions(self, fresh_metadata):
        """
        Update stored function metadata with the given fresh metadata.

        NOTE: THIS DOES NOT UPDATE INSTRUCTIONS

        Returns a map of function metadata that has been updated.
        """

        #
        # save the current node & function count before we merge in the delta
        # updates. this will enable us to very quickly tell if anything has
        # been added (versus updated)
        #

        node_count     = len(self.nodes)
        function_count = len(self.functions)

        #
        # the first step is to loop through the 'fresh' function metadata that
        # has been given to us, and identify what is truly new or different
        # from any existing metadata we hold.
        #

        for function_address, new_metadata in fresh_metadata.iteritems():

            # extract the 'old' metadata from the database metadata
            old_metadata = self.functions.get(
                function_address,
                self._blank_function
            )

            #
            # if the fresh metadata for this function is identical to the
            # existing metadata we have collected for it, there's nothing
            # else for us to do - just ignore it.
            #

            if old_metadata == new_metadata:
                continue

            # delete old nodes that explictly no longer exist
            nodes_removed = (old_metadata.nodes.viewkeys() -
                             new_metadata.nodes.viewkeys())
            for node_address in nodes_removed:
                del self.nodes[node_address]

            #
            # the newly collected metadata for a given function is empty, this
            # indicates that the function has been deleted. we go ahead and
            # remove its old function metadata from the db metadata entirely
            #

            if new_metadata.empty:
                del self.functions[function_address]
                continue

            # add or overwrite the new/updated basic blocks
            self.nodes.update(new_metadata.nodes)

            # save the new/updated function
            self.functions[function_address] = new_metadata

        #
        # if the function or node count has changed, we will know that
        # something must have been added, therefore our lookup lists will
        # need to be rebuilt/sorted. schedule a deferred refresh
        #

        if (node_count != len(self.nodes)) or (function_count != len(self.functions)):
            self._stale_lookup = True

    #--------------------------------------------------------------------------
    # Signal Handlers
    #--------------------------------------------------------------------------

    @mainthread
    def _name_changed(self, address, new_name, local_name=None):
        """
        Handler for rename event in IDA.

        TODO/FUTURE: refactor this to not be so IDA-specific
        """

        # we should never care about local renames (eg, loc_40804b), ignore
        if local_name or new_name.startswith("loc_"):
            return 0

        # get the function that this address falls within
        function = self.get_function(address)

        # if the address does not fall within a function (might happen?), ignore
        if not function:
            return 0

        #
        # ensure the renamed address matches the function start before
        # renaming the function in our metadata cache.
        #
        # I am not sure when this would not be the case (globals? maybe)
        # but I'd rather not find out.
        #

        if address != function.address:
            return

        # if the name isn't actually changing (misfire?) nothing to do
        if new_name == function.name:
            return

        logger.debug("Name changing @ 0x%X" % address)
        logger.debug("  Old name: %s" % function.name)
        logger.debug("  New name: %s" % new_name)

        # rename the function, and notify metadata listeners
        #function.name = new_name
        function.refresh_name()
        self._notify_function_renamed()

        # necessary for IDP/IDB_Hooks
        return 0

    #--------------------------------------------------------------------------
    # Callbacks
    #--------------------------------------------------------------------------

    def function_renamed(self, callback):
        """
        Subscribe a callback for function rename events.
        """
        register_callback(self._function_renamed_callbacks, callback)

    def _notify_function_renamed(self):
        """
        Notify listeners of a function rename event.
        """
        notify_callback(self._function_renamed_callbacks)

#------------------------------------------------------------------------------
# Function Level Metadata
#------------------------------------------------------------------------------

class FunctionMetadata(object):
    """
    Fast access function level metadata cache.
    """

    def __init__(self, address):

        # function metadata
        self.address = address
        self.name = None

        # node metadata
        self.nodes = {}
        self.edges = collections.defaultdict(list)

        # fixed/baked/computed metrics
        self.size = 0
        self.node_count = 0
        self.edge_count = 0
        self.instruction_count = 0
        self.cyclomatic_complexity = 0

        # collect metdata from the underlying database
        if address != -1:
            self._build_metadata()

    #--------------------------------------------------------------------------
    # Properties
    #--------------------------------------------------------------------------

    @property
    def instructions(self):
        """
        The instruction addresses in this function.
        """
        return set([ea for node in self.nodes.itervalues() for ea in node.instructions])

    @property
    def empty(self):
        """
        Return a bool indicating whether the object is populated.
        """
        return len(self.nodes) == 0

    #--------------------------------------------------------------------------
    # Public
    #--------------------------------------------------------------------------

    @disassembler.execute_read
    def refresh_name(self):
        """
        Refresh the function name against the open database.
        """
        self.name = disassembler.get_function_name_at(self.address)

    #--------------------------------------------------------------------------
    # Metadata Population
    #--------------------------------------------------------------------------

    def _build_metadata(self):
        """
        Collect function metadata from the underlying database.
        """
        self.name = disassembler.get_function_name_at(self.address)
        self._refresh_nodes()
        self._finalize()

    def _refresh_nodes(self):
        """
        TODO/COMMENT
        """
        raise RuntimeError("This function should have been monkey patched...")

    def _ida_refresh_nodes(self):
        """
        Refresh the function nodes against the open IDA database.

        NOTE: this function is disassembler-specific for optimal performance
        """

        # dispose of stale information
        function_metadata = self        # alias for code readability
        function_metadata.nodes = {}

        # get function & flowchart object from IDA database
        function  = idaapi.get_func(self.address)
        flowchart = idaapi.qflow_chart_t("", function, idaapi.BADADDR, idaapi.BADADDR, 0)

        #
        # now we will walk the flowchart for this function, collecting
        # information on each of its nodes (basic blocks) and populating
        # the function & node metadata objects.
        #

        for node_id in xrange(flowchart.size()):
            node = flowchart[node_id]

            # NOTE/COMPAT:
            if disassembler.using_ida7api:
                node_start = node.start_ea
                node_end   = node.end_ea
            else:
                node_start = node.startEA
                node_end   = node.endEA

            #
            # the node size as this flowchart sees it is 'zero'. This means
            # that another flowchart / function owns this node so we can just
            # ignore it.
            #

            if node_start == node_end:
                continue

            # create a new metadata object for this node
            node_metadata = NodeMetadata(node_start, node_end, node_id)

            #
            # establish a relationship between this node (basic block) and
            # this function metadata as its parent
            #

            node_metadata.function = function_metadata
            function_metadata.nodes[node_start] = node_metadata

        # enumerate the edges between nodes in the current function
        for node_metadata in function_metadata.nodes.itervalues():
            edge_src = node_metadata.instructions[-1]
            for edge_dst in idautils.CodeRefsFrom(edge_src, True):
                if edge_dst in function_metadata.nodes:
                    function_metadata.edges[edge_src].append(edge_dst)

    def _binja_refresh_nodes(self):
        """
        Refresh the function nodes against the open Binary Ninja database.

        NOTE: this function is disassembler-specific for optimal performance
        """

        # dispose of stale information
        function_metadata = self        # alias for code readability
        function_metadata.nodes = {}

        # get the function from the Binja database
        function = disassembler.bv.get_function_at(self.address)

        #
        # now we will walk the flowchart for this function, collecting
        # information on each of its nodes (basic blocks) and populating
        # the function & node metadata objects.
        #

        for node in function.basic_blocks:

            # create a new metadata object for this node
            node_metadata = NodeMetadata(node.start, node.end, node.index)

            #
            # establish a relationship between this node (basic block) and
            # this function metadata as its parent
            #

            node_metadata.function = function_metadata
            function_metadata.nodes[node.start] = node_metadata

            #
            # enumerate the edges produced by this node with a destination
            # that falls within this function.
            #

            edge_src = node_metadata.instructions[-1]
            for edge in node.outgoing_edges:
                function_metadata.edges[edge_src].append(edge.target.start)

    def _compute_complexity(self):
        """
        Walk the CFG to determine approximate cyclomatic complexity.

        This is mostly to account for IDA's inclusion of additional floating
        nodes in function flowcharts. These blocks tend to be for try/catch
        handlers, but can manifest in various other cases.

        Including these 'disembodied' blocks (often with no incoming edges)
        would radically throw off cyclomatic complexity computations. This
        routine instead walks the CFG from the function entry point so that
        only nodes/edges on the main graph are considered in CC.
        """
        to_walk = set([self.address])
        confirmed_nodes = set()
        confirmed_edges = {}

        while to_walk:

            # we want to find any edge that originates from here...
            node_address = to_walk.pop()
            confirmed_nodes.add(node_address)
            current_src = self.nodes[node_address].instructions[-1]

            # now we must loop through all remaining edges
            for node_address in self.edges[current_src]:

                # ignore nodes we have already confirmed (avoid loops)
                if node_address in confirmed_nodes:
                    continue

                # the destination must be new to continue walking, add it now
                to_walk.add(node_address)

            # update
            confirmed_edges[current_src] = self.edges.pop(current_src)

        # compute the final cyclomatic complexity
        num_edges = sum(len(x) for x in confirmed_edges.itervalues())
        num_nodes = len(confirmed_nodes)
        return num_edges - num_nodes + 2

    def _finalize(self):
        """
        Finalize function metadata for use.
        """
        self.size = sum(node.size for node in self.nodes.itervalues())
        self.node_count = len(self.nodes)
        self.edge_count = len(self.edges)
        self.instruction_count = sum(node.instruction_count for node in self.nodes.itervalues())
        self.cyclomatic_complexity = self._compute_complexity()

    #--------------------------------------------------------------------------
    # Operator Overloads
    #--------------------------------------------------------------------------

    def __eq__(self, other):
        """
        Compute function equality (==)
        """
        result = True
        result &= self.name == other.name
        result &= self.size == other.size
        result &= self.address == other.address
        result &= self.node_count == other.node_count
        result &= self.instruction_count == other.instruction_count
        result &= self.nodes.viewkeys() == other.nodes.viewkeys()
        return result

#------------------------------------------------------------------------------
# Node Level Metadata
#------------------------------------------------------------------------------

class NodeMetadata(object):
    """
    Fast access node level metadata cache.
    """

    def __init__(self, start_ea, end_ea, node_id=None):

        # node metadata
        self.size = end_ea - start_ea
        self.address = start_ea
        self.instruction_count = 0

        # flowchart node_id
        self.id = node_id

        # parent function_metadata
        self.function = None

        # instruction addresses
        self.instructions = []

        #----------------------------------------------------------------------

        # collect metdata from the underlying database
        self._build_metadata()

    #--------------------------------------------------------------------------
    # Metadata Population
    #--------------------------------------------------------------------------

    def _build_metadata(self):
        """
        TODO/COMMENT
        """
        raise RuntimeError("This function should have been monkey patched...")

    def _ida_build_metadata(self):
        """
        Collect node metadata from the underlying database.
        """
        current_address = self.address
        node_end = self.address + self.size

        #
        # loop through the node's entire range and count its instructions
        #   NOTE: we are assuming that every defined 'head' is an instruction
        #

        while current_address < node_end:
            instruction_size = idaapi.get_item_end(current_address) - current_address
            self.instructions.append(current_address)
            current_address += instruction_size

        # save the number of instructions in this block
        self.instruction_count = len(self.instructions)

    def _binja_build_metadata(self):
        """
        Collect node metadata from the underlying database.
        """
        bv = disassembler.bv
        current_address = self.address
        node_end = self.address + self.size

        while current_address < node_end:
            self.instructions.append(current_address)
            current_address += bv.get_instruction_length(current_address)

        # save the number of instructions in this block
        self.instruction_count = len(self.instructions)

    #--------------------------------------------------------------------------
    # Operator Overloads
    #--------------------------------------------------------------------------

    def __str__(self):
        """
        Printable NodeMetadata.
        """
        output  = ""
        output += "Node 0x%08X Info:\n" % self.address
        output += " Address: 0x%08X\n" % self.address
        output += " Size: %u\n" % self.size
        output += " Instruction Count: %u\n" % self.instruction_count
        output += " Id: %u\n" % self.id
        output += " Function: %s\n" % self.function
        output += " Instructions: %s" % self.instructions
        return output

    def __contains__(self, address):
        """
        Overload of 'in' keyword.

        Check if an address falls within a node (basic block).
        """
        if self.address <= address < self.address + self.size:
            return True
        return False

    def __eq__(self, other):
        """
        Compute node equality (==)
        """
        result = True
        result &= self.size == other.size
        result &= self.address == other.address
        result &= self.instruction_count == other.instruction_count
        result &= self.function == other.function
        result &= self.id == other.id
        return result

#------------------------------------------------------------------------------
# Metadata Helpers
#------------------------------------------------------------------------------

class MetadataDelta(object):
    """
    The computed delta between two DatabaseMetadata objects.
    """

    def __init__(self, new_metadata, old_metadata):

        # nodes
        self.nodes_added    = set()
        self.nodes_removed  = set()
        self.nodes_modified = set()

        # functions
        self.functions_added    = set()
        self.functions_removed  = set()
        self.functions_modified = set()
        self._dirty_functions   = set() # internal use only

        # compute the difference between the two metadata objects
        self._compute_delta(new_metadata, old_metadata)

    def _compute_delta(self, new_metadata, old_metadata):
        """
        Comptue the delta between two DatabaseMetadata objects.

        op1 is assumed to be the 'newer' / latest metadata, whereas op2
        is the 'older' / previous metadata.
        """
        assert isinstance(new_metadata, DatabaseMetadata)

        # accept an old_metadata of type 'None'
        if old_metadata is None:

            #
            # if the old metadata is 'None', we can assume *everything*
            # that may exist in the new_metadata must have been 'added'
            #

            self.nodes_added     = set(new_metadata.nodes.viewkeys())
            self.functions_added = set(new_metadata.functions.viewkeys())

            # nothing else to do
            return

        #
        # both new_metadata and old_metadata are real DatabaseMetadata objects
        # that we need to diff against each other, so compute their delta now
        #

        # compute the node delta
        self._compute_node_delta(new_metadata.nodes, old_metadata.nodes)

        # compute the function delta
        self._compute_function_delta(new_metadata.functions, old_metadata.functions)

        # done
        return

    def _compute_node_delta(self, new_nodes, old_nodes):
        """
        Compute the delta between two dictionaries of node metadata.
        """

        # loop through *all* the node addresses in both metadata objects
        all_node_addresses = new_nodes.viewkeys() | old_nodes.viewkeys()
        for node_address in all_node_addresses:

            # probe for this node in the metadata sets
            new_node_metadata = new_nodes.get(node_address, None)
            old_node_metadata = old_nodes.get(node_address, None)

            # the node does NOT exist in the new metadata, so it was deleted
            if not new_node_metadata:
                self.nodes_removed.add(node_address)
                self._dirty_functions |= set(old_node_metadata.functions.viewkeys())
                continue

            # the node does NOT exist in the old metadata, so it was added
            if not old_node_metadata:
                self.nodes_added.add(node_address)
                self._dirty_functions |= set(new_node_metadata.functions.viewkeys())
                continue

            #
            # ~ the node exists in *both* metadata sets ~
            #

            # if the nodes are identical, there's no delta (change)
            if new_node_metadata == old_node_metadata:
                continue

            # the nodes do not match, that's a difference!
            self.nodes_modified.add(node_address)
            self._dirty_functions |= set(new_node_metadata.functions.viewkeys())
            self._dirty_functions |= set(old_node_metadata.functions.viewkeys())

    def _compute_function_delta(self, new_functions, old_functions):
        """
        Compute the delta between two dictionaries of function metadata.
        """

        #
        # thanks to the work we did in _compute_node_delta, in theory we know
        # exactly which functions may have changed between the two metadata sets
        #
        # we loop through only these addresses, and bucketize them as needed
        #

        for function_address in self._dirty_functions:

            # probe for this function in the metadata sets
            new_func_metadata = new_functions.get(function_address, None)
            old_func_metadata = old_functions.get(function_address, None)

            # the function does NOT exist in the new metadata, so it was deleted
            if not new_func_metadata:
                self.functions_removed.add(function_address)
                continue

            # the function does NOT exist in the old metadata, so it was added
            if not old_func_metadata:
                self.functions_added.add(function_address)
                continue

            #
            # ~ the function exists in *both* metadata sets ~
            #

            #
            # in theory, every function that makes it this far given the
            # self._dirty_functions set should be different as one of its
            # underlying nodes were known to have changed...
            #

            self.functions_modified.add(function_address)

        # dispose of the dirty functions list as they're no longer needed
        self._dirty_functions = set()

    #--------------------------------------------------------------------------
    # Informational / DEBUG
    #--------------------------------------------------------------------------

    def dump_delta(self):
        """
        Dump the delta in human readable format.
        """
        self.dump_node_delta()
        self.dump_function_delta()

    def dump_node_delta(self):
        """
        Dump the computed node delta.
        """

        lmsg("Nodes added:")
        lmsg(hex_list(self.nodes_added))

        lmsg("Nodes removed:")
        lmsg(hex_list(self.nodes_removed))

        lmsg("Nodes modified:")
        lmsg(hex_list(self.nodes_modified))

    def dump_function_delta(self):
        """
        Dump the computed function delta.
        """

        lmsg("Functions added:")
        lmsg(hex_list(self.functions_added))

        lmsg("Functions removed:")
        lmsg(hex_list(self.functions_removed))

        lmsg("Functions modified:")
        lmsg(hex_list(self.functions_modified))

#--------------------------------------------------------------------------
# Async Metadata Helpers
#--------------------------------------------------------------------------

@disassembler.execute_read
def collect_function_metadata(function_addresses):
    """
    Collect function metadata for a list of addresses.
    """
    return { ea: FunctionMetadata(ea) for ea in function_addresses }

@disassembler.execute_ui
def metadata_progress(completed, total):
    """
    Handler for metadata collection callback, updates progress dialog.
    """
    disassembler.replace_wait_box(
        "Collected metadata for %u/%u Functions" % (completed, total)
    )

#--------------------------------------------------------------------------
# Shim HAX
#--------------------------------------------------------------------------
# TODO/COMMENT

if disassembler.NAME == "IDA":
    import idaapi
    import idautils

    # monkey patch
    FunctionMetadata._refresh_nodes = FunctionMetadata._ida_refresh_nodes
    NodeMetadata._build_metadata = NodeMetadata._ida_build_metadata

elif disassembler.NAME == "BINJA":
    import binaryninja

    # monky patch
    FunctionMetadata._refresh_nodes = FunctionMetadata._binja_refresh_nodes
    NodeMetadata._build_metadata = NodeMetadata._binja_build_metadata

else:
    raise RuntimeError("DISASSEMBLER-SPECIFIC SHIM MISSING")
