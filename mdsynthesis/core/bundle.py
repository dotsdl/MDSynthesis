"""
The Bundle object is the primary manipulator for Containers in aggregate.
They are returned as queries to Groups, Coordinators, and other Bundles. They
offer convenience methods for dealing with many Containers at once.

"""
import os

import aggregators
import persistence
import filesystem
import numpy as np
import multiprocessing as mp
import mdsynthesis as mds


class _CollectionBase(object):
    """Common interface elements for ordered sets of Containers.

    :class:`aggregators.Members` and :class:`Bundle` both use this interface.

    """
    def __len__(self):
        return len(self._list())

    def __getitem__(self, index):
        """Get member corresponding to the given index or slice.

        """
        if isinstance(index, int):
            out = self._list()[index]
        else:
            out = Bundle(*self._list()[index])

        return out

    def add(self, *containers):
        """Add any number of members to this collection.

        :Arguments:
            *containers*
                Sims and/or Groups to be added; may be a list of Sims and/or
                Groups; Sims or Groups can be given as either objects or paths
                to directories that contain object statefiles
        """
        outconts = list()
        for container in containers:
            if container is None:
                pass
            elif isinstance(container,
                            (list, tuple, Bundle, aggregators.Members)):
                self.add(*container)
            elif isinstance(container, mds.Container):
                outconts.append(container)
            elif os.path.exists(container):
                cont = filesystem.path2container(container)
                for c in cont:
                    outconts.append(c)

        for container in outconts:
            self._backend.add_member(container.uuid,
                                     container.containertype,
                                     container.basedir)

    def remove(self, *members, **kwargs):
        """Remove any number of members from the Group.

        :Arguments:
            *members*
                instances or indices of the members to remove

        :Keywords:
            *all*
                When True, remove all members [``False``]

        """
        uuids = self._backend.get_members_uuid()
        if kwargs.pop('all', False):
            remove = uuids
        else:
            remove = list()
            for member in members:
                if isinstance(member, int):
                    remove.append(uuids[member])
                elif isinstance(member, mds.containers.Container):
                    remove.append(member.uuid)
                else:
                    raise TypeError('Only an integer or container acceptable')

        self._backend.del_member(*remove)

        # remove from cache
        for uuid in remove:
            self._cache.pop(uuid, None)

    @property
    def containertypes(self):
        """Return a list of member containertypes.

        """
        return self._backend.get_members_containertype().tolist()

    @property
    def names(self):
        """Return a list of member names.

        Members that can't be found will have name ``None``.

        :Returns:
            *names*
                list giving the name of each member, in order;
                members that are missing will have name ``None``

        """
        names = list()
        for member in self._list():
            if member:
                names.append(member.name)
            else:
                names.append(None)

        return names

    @property
    def uuids(self):
        """Return a list of member uuids.

        :Returns:
            *uuids*
                list giving the uuid of each member, in order

        """
        return self._backend.get_members_uuid().tolist()

    def _list(self):
        """Return a list of members.

        Note: modifications of this list won't modify the members of the Group!

        Missing members will be present in the list as ``None``. This method is
        not intended for user-level use.

        """
        members = self._backend.get_members()
        uuids = members['uuid'].flatten().tolist()

        findlist = list()
        memberlist = list()

        for uuid in uuids:
            if uuid in self._cache and self._cache[uuid]:
                memberlist.append(self._cache[uuid])
            else:
                memberlist.append(None)
                findlist.append(uuid)

        # track down our non-cached containers
        paths = {path: members[path].flatten().tolist()
                 for path in self._backend.memberpaths}
        foxhound = filesystem.Foxhound(self, findlist, paths)
        foundconts = foxhound.fetch(as_containers=True)

        # add to cache, and ensure we get updated paths with a re-add
        # in case of an IOError, skip (probably due to permissions, but will
        # need something more robust later
        self._cache.update(foundconts)
        try:
            self.add(*foundconts.values())
        except IOError:
            pass

        # insert found containers into output list
        for uuid in findlist:
            result = foundconts[uuid]
            if not result:
                ind = list(members['uuid']).index(uuid)
                raise IOError("Could not find member" +
                              " {} (uuid: {});".format(ind, uuid) +
                              " re-add or remove it.")

            memberlist[list(uuids).index(uuid)] = result

        return memberlist

    @property
    def data(self):
        """Access the data of each member, collectively.

        """
        if not self._data:
            self._data = aggregators.MemberData(self)
        return self._data

    def map(self, function, processes=1, **kwargs):
        """Apply a function to each member, perhaps in parallel.

        A pool of processes is created for *processes* > 1; for example,
        with 40 members and 'processes=4', 4 processes will be created,
        each working on a single member at any given time. When each process
        completes work on a member, it grabs another, until no members remain.

        *kwargs* are passed to the given function

        :Arguments:
            *function*
                function to apply to each member; must take only a single
                container instance as input, but may take any number of keyword
                arguments

        :Keywords:
            *processes*
                how many processes to use; if 1, applies function to each
                member in member order

        :Returns:
            *results*
                list giving the result of the function for each member,
                in member order
        """
        if processes > 1:
            pool = mp.Pool(processes=processes)
            results = dict()
            for member in self:
                results[member.uuid] = pool.apply_async(
                        function, args=(member,), kwds=kwargs).get()
            pool.close()
            pool.join()

            # sort by member order
            results = [results[uuid] for uuid in self.uuids]
        else:
            results = [function(member, **kwargs) for member in self]

        return results


class _BundleBackend():
    """Backend class for Bundle.

    Has same interface as Group-specific components of
    :class:`persistence.GroupFile`. Behaves practically like an in-memory
    version of a state-file, but with only the components needed for the
    Bundle.

    """
    memberpaths = ['abspath']

    def __init__(self):
        # our table will be a structured array matching the schema of the
        # GroupFile _Members Table
        self.table = None

    def _member2record(self, uuid, containertype, basedir):
        """Return a record array from a member's information.

        This method defines the scheme for the Bundle's record array.

        """
        return np.array(
                (uuid, containertype, os.path.abspath(basedir)),
                dtype={'names': ['uuid', 'containertype', 'abspath'],
                       'formats': ['a{}'.format(persistence.uuidlength),
                                   'a{}'.format(persistence.namelength),
                                   'a{}'.format(persistence.pathlength)]
                       }).reshape(1, -1)

    def add_member(self, uuid, containertype, basedir):
        """Add a member to the Group.

        If the member is already present, its location will be updated with
        the given location.

        :Arguments:
            *uuid*
                the uuid of the new member
            *containertype*
                the container type of the new member (Sim or Group)
            *basedir*
                basedir of the new member in the filesystem

        """
        if self.table is None:
            self.table = self._member2record(uuid, containertype, basedir)
        else:
            # check if uuid already present
            index = np.where(self.table['uuid'] == uuid)[0]
            if index.size > 0:
                # if present, update location
                self.table[index[0]]['abspath'] = os.path.abspath(basedir)
            else:
                newmem = self._member2record(uuid, containertype, basedir)
                self.table = np.vstack((self.table, newmem))

    def del_member(self, *uuid, **kwargs):
        """Remove a member from the Group.

        :Arguments:
            *uuid*
                the uuid(s) of the member(s) to remove

        :Keywords:
            *all*
                When True, remove all members [``False``]

        """
        purge = kwargs.pop('all', False)

        if purge:
            self.table = None
        else:
            # remove redundant uuids from given list if present
            uuids = set([str(uid) for uid in uuid])

            # remove matching elements
            matches = list()
            for uuid in uuids:
                index = np.where(self.table['uuid'] == uuid)[0]
                if index:
                    matches.append(index)

            self.table = np.delete(self.table, matches)

    def get_member(self, uuid):
        """Get all stored information on the specified member.

        Returns a dictionary whose keys are column names and values the
        corresponding values for the member.

        :Arguments:
            *uuid*
                uuid of the member to retrieve information for

        :Returns:
            *memberinfo*
                a dictionary containing all information stored for the
                specified member
        """
        memberinfo = self.table[self.table[uuid] == uuid]

        if memberinfo:
            memberinfo = {x: memberinfo[x] for x in memberinfo.dtype.names}
        else:
            memberinfo = None

        return memberinfo

    def get_members(self):
        """Get full member table.

        Sometimes it is useful to read the whole member table in one go instead
        of doing multiple reads.

        :Returns:
            *memberdata*
                structured array giving full member data, with
                each row corresponding to a member
        """
        return self.table

    def get_members_uuid(self):
        """List uuid for each member.

        :Returns:
            *uuids*
                array giving containertype of each member, in order
        """
        return self.table['uuid'].flatten()

    def get_members_containertype(self):
        """List containertype for each member.

        :Returns:
            *containertypes*
                array giving containertype of each member, in order
        """
        return self.table['containertype'].flatten()

    def get_members_basedir(self):
        """List basedir for each member.

        :Returns:
            *basedirs*
                structured array containing all paths to member basedirs
        """
        return self.table['abspath'].flatten()


class Bundle(_CollectionBase):
    """Non-persistent Container for Sims and Groups.

    A Bundle is basically an indexable set. It is often used to return the
    results of a query on a Coordinator or a Group, but can be used on its
    own as well.

    """

    def __init__(self, *containers, **kwargs):
        """Generate a Bundle from any number of Containers.

        :Arguments:
            *containers*
                list giving either Sims, Groups, or paths giving the
                directories of the state files for such objects in the
                filesystem

        :Keywords:
            *flatten* [NOT IMPLEMENTED]
                if ``True``, will recursively obtain members of any Groups;
                only Sims will be present in the bunch

        """
        self._backend = _BundleBackend()
        self._cache = dict()
        self._data = None

        self.add(*containers)

    def __repr__(self):
        return "<Bundle({})>".format(self._list())
